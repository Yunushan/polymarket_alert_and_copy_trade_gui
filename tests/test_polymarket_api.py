from __future__ import annotations

import unittest
import io
import json
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

from polymarket import (
    bridge,
    clob_auth,
    clob_rest,
    data_api,
    gamma,
    http_client,
    relayer,
    ws_market,
    ws_sports,
    ws_transport,
    ws_user,
)
from polymarket.analytics_cache import (
    AnalyticsCacheConflictError,
    AnalyticsCacheDurabilityError,
    POLYMARKET_MDD_AUDIT_KIND,
    _fsync_parent_directory,
    load_analytics_cache,
    load_analytics_artifact,
    save_analytics_cache,
    mdd_payload_to_csv,
    store_analytics_artifact,
)
from polymarket.auth_readiness import build_clob_auth_readiness, validate_sdk_trading_readiness
from polymarket.coverage import polymarket_official_api_coverage
from polymarket.accounting import parse_accounting_snapshot_zip, reconcile_mdd_payload_with_accounting
from polymarket.credential_runbook import build_polymarket_credential_runbook
from polymarket.endpoints import ALL_POLYMARKET_ENDPOINTS, CLOB_ENDPOINTS, PolymarketEndpoint
from polymarket.http_client import PolymarketHTTPError, PolymarketRateLimitError, PolymarketValidationError
from polymarket.live_verification import (
    CONFIRM_LIVE_ORDER_CANCEL,
    LiveOrderCancelRequest,
    accepted_credential_read_checks,
    _same_account_authenticated_read_preflight,
    build_live_validation_stage_gates,
    build_live_order_cancel_plan,
    extract_order_id,
    run_live_order_cancel_verification,
)
from polymarket.live_reports import (
    LiveValidationStoreConflictError,
    LiveValidationStoreDurabilityError,
    LiveValidationStoreIntegrityError,
    LiveValidationStoreReadError,
    find_live_validation_report_duplicate,
    list_live_validation_coverage_promotion_proposal_snapshots,
    list_live_validation_reports,
    load_live_validation_decisions,
    load_live_validation_coverage_promotion_proposal_snapshot,
    load_live_validation_promotion_proposal_snapshots,
    load_live_validation_reports,
    live_validation_coverage_promotion_proposal,
    live_validation_coverage_promotion_proposal_hash,
    live_validation_coverage_promotion_proposal_markdown,
    live_validation_promotion_proposal_snapshot_diff_markdown,
    live_validation_promotion_proposal_snapshot_markdown,
    live_validation_report_payload_hash,
    live_validation_report_decisions_markdown,
    live_validation_report_promotion,
    live_validation_report_promotion_inventory,
    live_validation_report_review_bundle,
    live_validation_report_review_bundle_hash,
    live_validation_report_review_markdown,
    live_validation_report_summary,
    list_live_validation_report_decisions,
    purge_live_validation_coverage_promotion_proposal_snapshots,
    reconcile_live_validation_promotion_proposal_snapshot_idempotency,
    reconcile_live_validation_report_idempotency,
    record_live_validation_report_decision,
    save_live_validation_decisions,
    save_live_validation_promotion_proposal_snapshots,
    save_live_validation_reports,
    store_live_validation_coverage_promotion_proposal_snapshot,
    store_live_validation_report,
)
from polymarket.live_report_replay import replay_live_validation_report_paths
from polymarket.live_report_schema import (
    LiveValidationReportSchemaError,
    ensure_live_validation_report_valid,
    parse_live_validation_report_json,
    validate_live_validation_report,
)
from polymarket.trader import TraderConfig
from polymarket.ws_market import build_market_subscription
from polymarket.ws_sports import sports_ws_url
from polymarket.ws_user import build_user_subscription, probe_user_websocket, user_ws_url


HTTP_REQUEST = "polymarket.http_client.requests.request"
ROOT = Path(__file__).resolve().parent.parent
LIVE_REPORT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "polymarket" / "live_reports"


def clean_source_provenance(revision: str = "a" * 40) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "market-sentinel",
        "repository_origin": "github.com/yunushan/market-sentinel",
        "source_revision": revision,
        "initial_revision": revision,
        "final_revision": revision,
        "initial_clean": True,
        "final_clean": True,
        "stable": True,
    }


def successful_live_public_checks() -> dict[str, dict[str, object]]:
    return {
        "clob_time": {"status": "ok", "semantic_check": "current_unix_time"},
        "gamma_markets": {"status": "ok", "semantic_check": "market_identity"},
        "data_leaderboard": {"status": "ok", "semantic_check": "leaderboard_identity"},
        "bridge_supported_assets": {"status": "ok", "semantic_check": "supported_asset_identity"},
    }


def accepted_live_authenticated_reads() -> dict[str, dict[str, object]]:
    return {
        "clob_l2_orders": {
            "status": "ok",
            "detail": "Authenticated CLOB order list responded.",
            "sample_type": "list",
            "semantic_check": "authenticated_order_collection",
            "records_observed": 0,
        }
    }


def funded_live_safety_evidence(revision: str = "a" * 40) -> dict[str, object]:
    return {
        "account_authenticated_read_preflight": {
            "status": "pass",
            "same_trading_client": True,
            "account_identity_present": True,
            "sample_type": "list",
            "records_observed": 0,
        },
        "account_preflight": {
            "status": "pass",
            "sufficient_balance": True,
            "sufficient_allowance": True,
        },
        "execution_guards": {
            "status": "pass",
            "post_only": True,
            "time_in_force": "GTC",
            "maker_price_verified": True,
        },
        "geoblock_preflight": {"status": "pass", "blocked": False},
        "source_revision_gate": {
            "status": "pass",
            "clean": True,
            "matches_initial_revision": True,
            "source_revision": revision,
            "repository_origin": "github.com/yunushan/market-sentinel",
        },
    }


class LiveHarnessTraderSupport:
    def get_trading_account_address(self) -> str:
        return "0x" + "1" * 40

    def get_orders(self):
        return []

    def get_trading_balance_allowance(self, **_kwargs):
        return {"balance": "1000000", "allowances": {"exchange": "1000000"}}


def allowed_geoblock() -> dict[str, object]:
    return {"blocked": False, "country": "US", "region": "NY"}


CLOB_ORDER_ID = "0x" + "1" * 64
OTHER_CLOB_ORDER_ID = "0x" + "2" * 64


class FakeResponse:
    def __init__(
        self,
        payload,
        status_code: int = 200,
        *,
        headers=None,
        content: bytes | None = None,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content if content is not None else (text.encode("utf-8") if text else json.dumps(payload).encode())
        self.text = text
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


def request_url(mock_request) -> str:
    return mock_request.call_args.args[1]


L2_HEADERS = {
    "POLY_ADDRESS": "0xabc",
    "POLY_API_KEY": "key",
    "POLY_PASSPHRASE": "pass",
    "POLY_SIGNATURE": "sig",
    "POLY_TIMESTAMP": "1",
}


class PolymarketApiWrapperTests(unittest.TestCase):
    @staticmethod
    def _accounting_zip(equity_csv: str, positions_csv: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("equity.csv", equity_csv)
            archive.writestr("positions.csv", positions_csv)
        return buffer.getvalue()

    def test_analytics_cache_stores_prunes_and_loads_mdd_audit_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "analytics-cache.json"
            first = store_analytics_artifact(
                POLYMARKET_MDD_AUDIT_KIND,
                {"wallet": "0x1", "mode": "fast"},
                {"wallet": "0x1", "mdd_usd": 1.0, "points": []},
                path=cache_path,
                max_entries=1,
            )
            second = store_analytics_artifact(
                POLYMARKET_MDD_AUDIT_KIND,
                {"wallet": "0x2", "mode": "fast"},
                {"wallet": "0x2", "mdd_usd": 2.0, "points": []},
                path=cache_path,
                max_entries=1,
            )

            self.assertTrue(cache_path.exists())
            self.assertIsNone(load_analytics_artifact(first["key"], kind=POLYMARKET_MDD_AUDIT_KIND, path=cache_path))
            loaded = load_analytics_artifact(second["key"], kind=POLYMARKET_MDD_AUDIT_KIND, path=cache_path)

        self.assertIsNotNone(loaded)
        payload, metadata = loaded
        self.assertEqual(payload["wallet"], "0x2")
        self.assertTrue(metadata["hit"])

    def test_analytics_cache_atomically_saves_and_quarantines_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "analytics-cache.json"
            save_analytics_cache({"entries": {"one": {"kind": "test"}}}, cache_path)
            self.assertEqual(load_analytics_cache(cache_path)["entries"]["one"]["kind"], "test")
            self.assertFalse(list(cache_path.parent.glob("*.tmp")))

            cache_path.write_text("{ not valid json", encoding="utf-8")
            self.assertEqual(load_analytics_cache(cache_path)["entries"], {})
            self.assertFalse(cache_path.exists())
            backups = list(cache_path.parent.glob("analytics-cache.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ not valid json")

    def test_analytics_cache_rejects_stale_direct_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "analytics-cache.json"
            save_analytics_cache({"entries": {}}, cache_path)
            first_writer = load_analytics_cache(cache_path)
            stale_writer = load_analytics_cache(cache_path)

            first_writer["entries"]["first"] = {"kind": "test"}
            save_analytics_cache(first_writer, cache_path)
            stale_writer["entries"]["stale"] = {"kind": "test"}

            with self.assertRaises(AnalyticsCacheConflictError):
                save_analytics_cache(stale_writer, cache_path)

            self.assertEqual(set(load_analytics_cache(cache_path)["entries"]), {"first"})

    def test_analytics_cache_post_replace_failure_is_explicit_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "analytics-cache.json"
            cache = {"entries": {"committed": {"kind": "test"}}}

            with (
                patch("polymarket.analytics_cache._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(AnalyticsCacheDurabilityError) as raised,
            ):
                save_analytics_cache(cache, cache_path)

            self.assertTrue(raised.exception.committed)
            self.assertEqual(raised.exception.path, cache_path)
            self.assertIn("committed", load_analytics_cache(cache_path)["entries"])

            # Even a plain mapping has an idempotent exact-byte retry path;
            # it cannot silently overwrite a different intervening revision.
            save_analytics_cache(cache, cache_path)
            self.assertEqual(set(load_analytics_cache(cache_path)["entries"]), {"committed"})

    def test_analytics_cache_serializes_concurrent_thread_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "analytics-cache.json"
            barrier = threading.Barrier(8)
            errors = []

            def store(index: int) -> None:
                try:
                    barrier.wait(timeout=10)
                    store_analytics_artifact(
                        "thread-test",
                        {"writer": index},
                        {"writer": index},
                        path=cache_path,
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)

            threads = [threading.Thread(target=store, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            entries = load_analytics_cache(cache_path)["entries"]
            self.assertEqual(len(entries), len(threads))
            self.assertEqual({entry["payload"]["writer"] for entry in entries.values()}, set(range(8)))

    def test_analytics_cache_serializes_concurrent_process_writers(self) -> None:
        worker_script = """
import sys
import time
from pathlib import Path

from polymarket.analytics_cache import store_analytics_artifact

target = Path(sys.argv[1])
start_flag = Path(sys.argv[2])
writer = int(sys.argv[3])
while not start_flag.exists():
    time.sleep(0.005)
store_analytics_artifact(
    "process-test",
    {"writer": writer},
    {"writer": writer},
    path=target,
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_path = root / "analytics-cache.json"
            start_flag = root / "start"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", worker_script, str(cache_path), str(start_flag), str(index)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(4)
            ]
            try:
                start_flag.touch()
                for process in processes:
                    stdout, stderr = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

            entries = load_analytics_cache(cache_path)["entries"]
            self.assertEqual(len(entries), len(processes))
            self.assertEqual({entry["payload"]["writer"] for entry in entries.values()}, set(range(4)))

    def test_analytics_cache_parent_directory_is_synced_on_posix(self) -> None:
        path = Path("cache") / "analytics-cache.json"
        with (
            patch("polymarket.analytics_cache.os.name", "posix"),
            patch("polymarket.analytics_cache.os.open", return_value=42) as open_directory,
            patch("polymarket.analytics_cache.os.fsync") as sync,
            patch("polymarket.analytics_cache.os.close") as close,
        ):
            _fsync_parent_directory(path)

        open_directory.assert_called_once()
        sync.assert_called_once_with(42)
        close.assert_called_once_with(42)

    def test_live_safety_stores_use_secure_atomic_artifact_writes(self) -> None:
        writers = (
            ("reports", "reports", save_live_validation_reports),
            ("decisions", "decisions", save_live_validation_decisions),
            ("snapshots", "snapshots", save_live_validation_promotion_proposal_snapshots),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, collection_key, writer in writers:
                with self.subTest(store=name):
                    target = root / f"{name}.json"
                    predictable_temporary = target.with_name(f"{target.name}.tmp")
                    predictable_temporary.write_text("do not overwrite", encoding="utf-8")
                    with patch("polymarket.live_reports._fsync_parent_directory") as sync_parent:
                        self.assertEqual(writer({collection_key: {"one": {"status": "ok"}}}, target), target)

                    self.assertEqual(
                        json.loads(target.read_text(encoding="utf-8"))[collection_key]["one"]["status"],
                        "ok",
                    )
                    self.assertEqual(predictable_temporary.read_text(encoding="utf-8"), "do not overwrite")
                    self.assertFalse(list(root.glob(f".{target.name}.*.tmp")))
                    sync_parent.assert_called_once_with(target)

    def test_live_safety_stores_fail_closed_and_preserve_corrupt_json(self) -> None:
        loaders_and_writers = (
            ("reports", "reports", load_live_validation_reports, save_live_validation_reports),
            ("decisions", "decisions", load_live_validation_decisions, save_live_validation_decisions),
            (
                "snapshots",
                "snapshots",
                load_live_validation_promotion_proposal_snapshots,
                save_live_validation_promotion_proposal_snapshots,
            ),
        )
        corrupt_payload = b'{"evidence": "must survive", broken'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, collection_key, loader, writer in loaders_and_writers:
                with self.subTest(store=name):
                    target = root / f"{name}.json"
                    target.write_bytes(corrupt_payload)

                    with self.assertRaises(LiveValidationStoreReadError) as load_error:
                        loader(target)
                    self.assertEqual(load_error.exception.path, target)
                    self.assertNotIn("must survive", str(load_error.exception))

                    with self.assertRaises(LiveValidationStoreReadError):
                        writer({collection_key: {"replacement": True}}, target)
                    self.assertEqual(target.read_bytes(), corrupt_payload)
                    self.assertFalse(list(root.glob(f".{target.name}.*.tmp")))

            report_target = root / "report-mutation.json"
            report_target.write_bytes(corrupt_payload)
            valid_report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
            with self.assertRaises(LiveValidationStoreReadError):
                store_live_validation_report(valid_report, path=report_target)
            self.assertEqual(report_target.read_bytes(), corrupt_payload)

    def test_live_safety_stores_reject_stale_direct_writers(self) -> None:
        stores = (
            ("reports", "reports", load_live_validation_reports, save_live_validation_reports),
            ("decisions", "decisions", load_live_validation_decisions, save_live_validation_decisions),
            (
                "snapshots",
                "snapshots",
                load_live_validation_promotion_proposal_snapshots,
                save_live_validation_promotion_proposal_snapshots,
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, collection_key, loader, writer in stores:
                with self.subTest(store=name):
                    target = root / f"{name}.json"
                    initial = loader(target)
                    initial[collection_key]["initial"] = {"status": "ok"}
                    writer(initial, target)

                    stale = loader(target)
                    fresh = loader(target)
                    fresh[collection_key]["fresh"] = {"status": "committed"}
                    writer(fresh, target)
                    stale[collection_key]["stale"] = {"status": "must-not-overwrite"}

                    with self.assertRaises(LiveValidationStoreConflictError):
                        writer(stale, target)

                    persisted = loader(target)[collection_key]
                    self.assertIn("fresh", persisted)
                    self.assertNotIn("stale", persisted)

    def test_live_safety_store_exact_bytes_retry_after_uncertain_directory_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "reports.json"
            plain_snapshot = {"reports": {"one": {"status": "committed"}}}
            with (
                patch("polymarket.live_reports._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(LiveValidationStoreDurabilityError) as raised,
            ):
                save_live_validation_reports(plain_snapshot, target)

            self.assertTrue(raised.exception.committed)
            self.assertEqual(load_live_validation_reports(target)["reports"]["one"]["status"], "committed")
            with patch("polymarket.live_reports._fsync_parent_directory") as sync_parent:
                self.assertEqual(save_live_validation_reports(plain_snapshot, target), target)
            sync_parent.assert_called_once_with(target)

    def test_live_report_idempotency_reconciles_uncertain_commit_and_binds_request(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        raw_idempotency_key = "report-import-2026-08-26T12:00:00Z"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "reports.json"
            with (
                patch("polymarket.live_reports._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(LiveValidationStoreDurabilityError) as raised,
            ):
                store_live_validation_report(
                    valid,
                    source="durability_test",
                    label="committed once",
                    path=target,
                    idempotency_key=raw_idempotency_key,
                )

            self.assertTrue(raised.exception.committed)
            self.assertTrue(raised.exception.operation_key)
            self.assertTrue(raised.exception.idempotency_key_hash)
            self.assertNotIn(raw_idempotency_key, str(raised.exception))
            self.assertNotIn(raw_idempotency_key, target.read_text(encoding="utf-8"))

            with patch("polymarket.live_reports._fsync_parent_directory") as sync_parent:
                replay = store_live_validation_report(
                    valid,
                    source="durability_test",
                    label="committed once",
                    path=target,
                    idempotency_key=raw_idempotency_key,
                )
            sync_parent.assert_called_once_with(target)
            self.assertFalse(replay["stored"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(list_live_validation_reports(path=target)["counts"]["entries"], 1)

            with self.assertRaisesRegex(ValueError, "different operation request"):
                store_live_validation_report(
                    valid,
                    source="durability_test",
                    label="different request",
                    path=target,
                    idempotency_key=raw_idempotency_key,
                )
            with self.assertRaisesRegex(ValueError, "visible ASCII"):
                store_live_validation_report(valid, path=target, idempotency_key="contains a space")
            for malformed_key in (" leading", "trailing "):
                with self.subTest(idempotency_key=malformed_key):
                    with self.assertRaisesRegex(ValueError, "visible ASCII"):
                        store_live_validation_report(valid, path=target, idempotency_key=malformed_key)

            malformed_store = load_live_validation_reports(target)
            original_entry = next(iter(malformed_store["reports"].values()))
            ambiguous_entry = json.loads(json.dumps(original_entry))
            ambiguous_entry["key"] = "ambiguous-copy"
            malformed_store["reports"]["ambiguous-copy"] = ambiguous_entry
            save_live_validation_reports(malformed_store, target)
            bytes_before_rejection = target.read_bytes()
            with self.assertRaisesRegex(LiveValidationStoreIntegrityError, "ambiguous across multiple records"):
                store_live_validation_report(
                    valid,
                    source="durability_test",
                    label="committed once",
                    path=target,
                    idempotency_key=raw_idempotency_key,
                )
            self.assertEqual(target.read_bytes(), bytes_before_rejection)

    def test_duplicate_report_idempotency_preserves_outcome_and_single_audit(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "reports.json"
            stored = store_live_validation_report(valid, path=target, label="original")
            duplicate_args = {
                "report": valid,
                "source": "retry_test",
                "label": "duplicate import",
                "path": target,
                "idempotency_key": "duplicate-import-1",
            }
            with (
                patch("polymarket.live_reports._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(LiveValidationStoreDurabilityError),
            ):
                store_live_validation_report(**duplicate_args)

            committed_entry = load_live_validation_reports(target)["reports"][stored["key"]]
            self.assertEqual(committed_entry["duplicate_import_count"], 1)
            replay = store_live_validation_report(**duplicate_args)
            self.assertTrue(replay["idempotent_replay"])
            self.assertTrue(replay["duplicate"])
            self.assertEqual(replay["duplicate_policy"], "skip")
            self.assertEqual(replay["duplicate_of"], stored["key"])
            persisted_entry = load_live_validation_reports(target)["reports"][stored["key"]]
            self.assertEqual(persisted_entry["duplicate_import_count"], 1)

    def test_caller_bound_report_idempotency_ignores_regenerated_payload_drift(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        regenerated = json.loads(json.dumps(valid))
        regenerated["generated_at"] = float(valid["generated_at"]) + 60.0
        binding = {
            "mode": "generated",
            "source": "react_generated",
            "label": "operator probe",
            "duplicate_policy": "skip",
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "reports.json"
            first = store_live_validation_report(
                valid,
                source="react_generated",
                label="operator probe",
                path=target,
                idempotency_key="generated-report-1",
                idempotency_request=binding,
            )
            preflight = reconcile_live_validation_report_idempotency(
                idempotency_key="generated-report-1",
                idempotency_request=binding,
                path=target,
            )
            self.assertIsNotNone(preflight)
            self.assertTrue(preflight["idempotent_replay"])
            self.assertEqual(preflight["key"], first["key"])
            replay = store_live_validation_report(
                regenerated,
                source="react_generated",
                label="operator probe",
                path=target,
                idempotency_key="generated-report-1",
                idempotency_request=binding,
            )
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["key"], first["key"])
            self.assertEqual(list_live_validation_reports(path=target)["counts"]["entries"], 1)

    def test_malformed_idempotency_bindings_fail_closed_without_mutation(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            malformed_list_path = root / "malformed-list.json"
            first = store_live_validation_report(valid, path=malformed_list_path)
            malformed_list = load_live_validation_reports(malformed_list_path)
            malformed_list["reports"][first["key"]]["idempotency_key_hashes"] = "not-a-list"
            save_live_validation_reports(malformed_list, malformed_list_path)
            bytes_before = malformed_list_path.read_bytes()
            with self.assertRaisesRegex(LiveValidationStoreIntegrityError, "key bindings are malformed"):
                store_live_validation_report(
                    valid,
                    path=malformed_list_path,
                    idempotency_key="new-duplicate-binding",
                )
            self.assertEqual(malformed_list_path.read_bytes(), bytes_before)

            contradictory_path = root / "contradictory.json"
            bound = store_live_validation_report(
                valid,
                path=contradictory_path,
                idempotency_key="bound-request",
            )
            contradictory = load_live_validation_reports(contradictory_path)
            contradictory["reports"][bound["key"]]["idempotency_request_hash"] = "0" * 64
            save_live_validation_reports(contradictory, contradictory_path)
            bytes_before = contradictory_path.read_bytes()
            with self.assertRaisesRegex(LiveValidationStoreIntegrityError, "contradict each other"):
                store_live_validation_report(
                    valid,
                    path=contradictory_path,
                    idempotency_key="bound-request",
                )
            self.assertEqual(contradictory_path.read_bytes(), bytes_before)

    def test_live_decision_and_snapshot_idempotency_reconcile_uncertain_commits(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "reports.json"
            decision_path = root / "decisions.json"
            snapshot_path = root / "snapshots.json"
            stored = store_live_validation_report(valid, path=report_path, label="decision source")
            bundle = live_validation_report_review_bundle(stored["key"], path=report_path)
            self.assertIsNotNone(bundle)

            decision_args = {
                "report_key": stored["key"],
                "payload_hash": stored["payload_hash"],
                "target_tier": "public_live_verified",
                "decision": "rejected",
                "reviewer_note": "External proof is still missing.",
                "review_bundle_hash": bundle["review_bundle_hash"],
                "reviewer": "production-operator",
                "report_store_path": report_path,
                "decision_path": decision_path,
                "idempotency_key": "decision-2026-08-26-1",
            }
            with (
                patch("polymarket.live_reports._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(LiveValidationStoreDurabilityError) as decision_error,
            ):
                record_live_validation_report_decision(**decision_args)
            self.assertTrue(decision_error.exception.operation_key)
            store_live_validation_report(
                valid,
                path=report_path,
                label="later duplicate changes the review bundle",
            )
            with patch("polymarket.live_reports._fsync_parent_directory") as sync_parent:
                decision_replay = record_live_validation_report_decision(**decision_args)
            sync_parent.assert_called_once_with(decision_path)
            self.assertTrue(decision_replay["idempotent_replay"])
            self.assertEqual(len(load_live_validation_decisions(decision_path)["decisions"]), 1)

            proposal = live_validation_coverage_promotion_proposal(
                report_store_path=report_path,
                decision_path=decision_path,
            )
            snapshot_args = {
                "proposal": proposal,
                "report_store_path": report_path,
                "decision_path": decision_path,
                "path": snapshot_path,
                "source": "durability_test",
                "label": "promotion snapshot",
                "idempotency_key": "snapshot-2026-08-26-1",
                "idempotency_request": {
                    "target_tier": "",
                    "source": "durability_test",
                    "label": "promotion snapshot",
                },
            }
            with (
                patch("polymarket.live_reports._fsync_parent_directory", side_effect=OSError("sync failed")),
                self.assertRaises(LiveValidationStoreDurabilityError) as snapshot_error,
            ):
                store_live_validation_coverage_promotion_proposal_snapshot(**snapshot_args)
            self.assertTrue(snapshot_error.exception.operation_key)
            with patch("polymarket.live_reports._fsync_parent_directory") as sync_parent:
                snapshot_replay = store_live_validation_coverage_promotion_proposal_snapshot(**snapshot_args)
            sync_parent.assert_called_once_with(snapshot_path)
            self.assertTrue(snapshot_replay["idempotent_replay"])
            self.assertEqual(
                len(load_live_validation_promotion_proposal_snapshots(snapshot_path)["snapshots"]),
                1,
            )
            snapshot_preflight = reconcile_live_validation_promotion_proposal_snapshot_idempotency(
                idempotency_key=snapshot_args["idempotency_key"],
                idempotency_request=snapshot_args["idempotency_request"],
                report_store_path=report_path,
                decision_path=decision_path,
                path=snapshot_path,
            )
            self.assertIsNotNone(snapshot_preflight)
            self.assertEqual(snapshot_preflight["key"], snapshot_replay["key"])

            regenerated_proposal = json.loads(json.dumps(proposal))
            regenerated_proposal.pop("proposal_hash", None)
            regenerated_proposal["generated_at"] = int(regenerated_proposal.get("generated_at") or 0) + 60
            regenerated_args = dict(snapshot_args)
            regenerated_args["proposal"] = regenerated_proposal
            regenerated_replay = store_live_validation_coverage_promotion_proposal_snapshot(**regenerated_args)
            self.assertTrue(regenerated_replay["idempotent_replay"])
            self.assertEqual(regenerated_replay["key"], snapshot_replay["key"])

    def test_live_decision_keys_do_not_overwrite_same_clock_audit_entries(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "reports.json"
            decision_path = root / "decisions.json"
            stored = store_live_validation_report(valid, path=report_path)
            bundle = live_validation_report_review_bundle(stored["key"], path=report_path)
            decision_args = {
                "report_key": stored["key"],
                "payload_hash": stored["payload_hash"],
                "target_tier": "public_live_verified",
                "decision": "rejected",
                "reviewer_note": "Keep both operator audit events.",
                "review_bundle_hash": bundle["review_bundle_hash"],
                "report_store_path": report_path,
                "decision_path": decision_path,
            }
            with (
                patch("polymarket.live_reports._now", return_value=1_780_000_000),
                patch("polymarket.live_reports.time.time_ns", return_value=123_456_789),
            ):
                first = record_live_validation_report_decision(**decision_args)
                second = record_live_validation_report_decision(**decision_args)

            self.assertNotEqual(first["key"], second["key"])
            self.assertEqual(len(load_live_validation_decisions(decision_path)["decisions"]), 2)

    def test_live_report_store_serializes_concurrent_process_writers(self) -> None:
        worker_script = """
import json
import sys
import time
from pathlib import Path

from polymarket.live_reports import store_live_validation_report

target = Path(sys.argv[1])
fixture = Path(sys.argv[2])
start_flag = Path(sys.argv[3])
label = sys.argv[4]
while not start_flag.exists():
    time.sleep(0.005)
payload = json.loads(fixture.read_text(encoding="utf-8"))
store_live_validation_report(
    payload,
    source="concurrent_process_test",
    label=label,
    path=target,
    allow_duplicate=True,
)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "reports.json"
            start_flag = root / "start"
            fixture = LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", worker_script, str(target), str(fixture), str(start_flag), f"writer-{index}"],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(4)
            ]
            try:
                start_flag.touch()
                for process in processes:
                    stdout, stderr = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

            listing = list_live_validation_reports(path=target)
            self.assertEqual(listing["counts"]["entries"], len(processes))
            self.assertEqual({entry["label"] for entry in listing["entries"]}, {f"writer-{index}" for index in range(4)})

    def test_analytics_cache_does_not_quarantine_a_directory_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache-directory"
            cache_path.mkdir()

            with self.assertRaises(OSError):
                load_analytics_cache(cache_path)
            self.assertTrue(cache_path.is_dir())
            self.assertFalse(list(cache_path.parent.glob("cache-directory.corrupt-*")))

    def test_mdd_payload_to_csv_exports_summary_and_points(self) -> None:
        csv_text = mdd_payload_to_csv(
            {
                "wallet": "0xabc",
                "mdd_method": "test",
                "mdd_available": True,
                "mdd_usd": 12.5,
                "mdd_pct": 4.2,
                "equity_base_usd": 100.0,
                "peak_value": 20.0,
                "trough_value": 7.5,
                "mdd_pct_basis": "test_basis",
                "points": [{"timestamp": 10, "value": 20.0, "source": "closed_position"}],
            }
        )

        self.assertIn("section,wallet,mdd_method", csv_text)
        self.assertIn("summary,0xabc,test", csv_text)
        self.assertIn("point,0xabc,test,10,20.0", csv_text)

    def test_parse_market_outcomes_handles_json_encoded_fields(self) -> None:
        outcomes = gamma.parse_market_outcomes(
            {
                "clobTokenIds": '["token-yes", "token-no"]',
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.61", "0.39"]',
            }
        )

        self.assertEqual([o.outcome for o in outcomes], ["Yes", "No"])
        self.assertEqual([o.token_id for o in outcomes], ["token-yes", "token-no"])
        self.assertEqual([o.price for o in outcomes], [0.61, 0.39])

    def test_best_bid_ask_accepts_books_and_legacy_buy_sell_names(self) -> None:
        self.assertEqual(
            clob_rest.best_bid_ask_from_book(
                {"bids": [{"price": "0.49"}], "asks": [{"price": "0.51"}]}
            ),
            (0.49, 0.51),
        )
        self.assertEqual(
            clob_rest.best_bid_ask_from_book(
                {"buys": [{"price": "0.48"}], "sells": [{"price": "0.52"}]}
            ),
            (0.48, 0.52),
        )
        self.assertEqual(
            clob_rest.best_bid_ask_from_book(
                {
                    "bids": [{"price": "0.10"}, {"price": "0.47"}, {"price": "nan"}],
                    "asks": [{"price": "0.90"}, {"price": "0.53"}, {"price": "inf"}],
                }
            ),
            (0.47, 0.53),
        )

    def test_live_order_ids_require_supported_documented_hash_shapes(self) -> None:
        uppercase_hash = "0x" + "A" * 64
        self.assertEqual(extract_order_id({"orderID": uppercase_hash}), "0x" + "a" * 64)
        self.assertEqual(extract_order_id({"orderID": "0x" + "b" * 40}), "0x" + "b" * 40)
        for invalid in ("order-1", "1" * 64, "0x" + "g" * 64, "0x" + "1" * 63):
            with self.subTest(invalid=invalid):
                self.assertEqual(extract_order_id({"orderID": invalid}), "")

    def test_same_client_order_read_requires_a_success_list_schema(self) -> None:
        class Reader:
            def get_trading_account_address(self):
                return "0x" + "1" * 40

            def get_orders(self):
                return {"error": "unauthorized"}

        with self.assertRaisesRegex(ValueError, "documented list shape"):
            _same_account_authenticated_read_preflight(Reader())

        Reader.get_orders = lambda self: [{"orderID": "bad-id"}]
        with self.assertRaisesRegex(ValueError, "invalid order record"):
            _same_account_authenticated_read_preflight(Reader())

        Reader.get_orders = lambda self: [{"orderID": CLOB_ORDER_ID}]
        preflight, _ = _same_account_authenticated_read_preflight(Reader())
        self.assertEqual(preflight["records_observed"], 1)

    def test_get_midpoint_accepts_dict_payload(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"midpoint": "0.42"})) as mock_get:
            midpoint = clob_rest.get_midpoint("token-1", timeout=3)

        self.assertEqual(midpoint, 0.42)
        self.assertEqual(mock_get.call_args.kwargs["params"], {"token_id": "token-1"})
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 3)

    def test_get_last_trade_price_accepts_dict_payload(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"price": "0.58"})) as mock_get:
            price = clob_rest.get_last_trade_price("token-1", timeout=3)

        self.assertEqual(price, 0.58)
        self.assertIn("/last-trade-price", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"], {"token_id": "token-1"})
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 3)

    def test_activity_request_clamps_limit_and_offset_and_passes_filters(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse([{"id": 1}])) as mock_get:
            result = data_api.get_activity(
                "0xabc",
                limit=999,
                offset=-5,
                types=["TRADE"],
                side="BUY",
                market=["condition-1"],
                start=10,
                end=20,
                sort_direction="ASC",
                timeout=4,
            )

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(params["limit"], 500)
        self.assertEqual(params["offset"], 0)
        self.assertEqual(params["type"], ["TRADE"])
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["market"], ["condition-1"])
        self.assertEqual(params["start"], 10)
        self.assertEqual(params["end"], 20)
        self.assertEqual(params["sortBy"], "TIMESTAMP")
        self.assertEqual(params["sortDirection"], "ASC")
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 4)

    def test_leaderboard_request_clamps_page_and_accepts_wrapped_payload(self) -> None:
        payload = {"data": [{"proxyWallet": "0xabc", "pnl": "12", "volume": "120"}]}
        with patch(HTTP_REQUEST, return_value=FakeResponse(payload)) as mock_get:
            result = data_api.get_leaderboard(
                limit=100,
                offset=-2,
                sort_by="ROI",
                sort_direction="SIDEWAYS",
                period="all",
                timeout=5,
            )

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(result, payload["data"])
        self.assertIn("/v1/leaderboard", request_url(mock_get))
        self.assertEqual(params["limit"], 50)
        self.assertEqual(params["offset"], 0)
        self.assertEqual(params["orderBy"], "PNL")
        self.assertEqual(params["sortDirection"], "DESC")
        self.assertEqual(params["timePeriod"], "ALL")
        self.assertEqual(params["category"], "OVERALL")
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 5)

    def test_leaderboard_request_rejects_offsets_beyond_the_documented_source(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"data": []})) as mock_get:
            with self.assertRaisesRegex(ValueError, "offset must not exceed 1000"):
                data_api.get_leaderboard(offset=2_500_000)
        mock_get.assert_not_called()

    def test_closed_positions_request_uses_public_profile_endpoint(self) -> None:
        payload = [{"asset": "token-yes", "realizedPnl": "-12", "timestamp": 10}]
        with patch(HTTP_REQUEST, return_value=FakeResponse(payload)) as mock_get:
            result = data_api.get_closed_positions(
                "0xabc",
                limit=99,
                offset=-1,
                sort_by="bad",
                sort_direction="bad",
                timeout=6,
            )

        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(result, payload)
        self.assertIn("/closed-positions", request_url(mock_get))
        self.assertEqual(params["limit"], 50)
        self.assertEqual(params["offset"], 0)
        self.assertEqual(params["sortBy"], "TIMESTAMP")
        self.assertEqual(params["sortDirection"], "ASC")
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 6)

    def test_accounting_snapshot_zip_parser_extracts_equity_positions_and_cashflow(self) -> None:
        raw = self._accounting_zip(
            "timestamp,equity,deposits,withdrawals\n"
            "10,1000,1000,0\n"
            "20,1200,0,0\n"
            "30,900,0,100\n",
            "asset,currentValue,realizedPnl,cashPnl,initialValue\n"
            "token-1,250,12,3,200\n"
            "token-2,100,-5,-1,90\n",
        )

        snapshot = parse_accounting_snapshot_zip(raw)

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["equity"]["base_equity_usd"], 1200.0)
        self.assertEqual(snapshot["equity"]["cash_flows"]["net_cash_flow_usd"], 900.0)
        self.assertEqual(snapshot["equity"]["cash_flows"]["cash_flow_gap_usd"], -1000.0)
        self.assertAlmostEqual(snapshot["positions"]["current_value_usd"], 350.0)
        self.assertAlmostEqual(snapshot["positions"]["realized_pnl_usd"], 7.0)

    def test_accounting_snapshot_reconciliation_overrides_mdd_percentage_base(self) -> None:
        snapshot = parse_accounting_snapshot_zip(
            self._accounting_zip(
                "timestamp,equity\n10,1000\n20,1200\n",
                "asset,currentValue,realizedPnl\nasset-1,40,50\n",
            )
        )
        payload = {
            "mdd_usd": 50.0,
            "mdd_pct": 25.0,
            "peak_value": 100.0,
            "points": [{"value": 100.0}, {"value": 50.0}],
            "points_total": 2,
            "equity_base_usd": 100.0,
            "open_current_value": 40.0,
            "cumulative_realized_pnl": 50.0,
        }

        reconciled = reconcile_mdd_payload_with_accounting(payload, snapshot)

        self.assertEqual(reconciled["equity_base_source"], "accounting_snapshot_max_equity")
        self.assertEqual(reconciled["equity_base_usd"], 1200.0)
        self.assertAlmostEqual(reconciled["mdd_pct"], 50.0 / 1300.0 * 100.0)
        self.assertEqual(reconciled["accounting_snapshot"]["reconciliation"]["status"], "reconciled")
        self.assertTrue(reconciled["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_clob_public_wrappers_cover_batch_and_history_endpoints(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse([{"asset_id": "token-1"}])) as mock_post:
            books = clob_rest.get_books(["token-1"], timeout=2)
        self.assertEqual(books, [{"asset_id": "token-1"}])
        self.assertEqual(mock_post.call_args.args[0], "POST")
        self.assertIn("/books", request_url(mock_post))
        self.assertEqual(mock_post.call_args.kwargs["json"], [{"token_id": "token-1"}])

        with patch(HTTP_REQUEST, return_value=FakeResponse({"history": []})) as mock_get:
            history = clob_rest.get_price_history("asset-1", start_ts=1, end_ts=2, interval="1h", fidelity=5)
        self.assertEqual(history, {"history": []})
        self.assertIn("/prices-history", request_url(mock_get))
        self.assertEqual(
            mock_get.call_args.kwargs["params"],
            {"market": "asset-1", "startTs": 1, "endTs": 2, "interval": "1h", "fidelity": 5},
        )

    def test_clob_public_rewards_builder_and_market_parameter_wrappers(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"next_cursor": "LTE="})) as mock_get:
            rewards = clob_rest.get_current_rewards_config(sponsored=True, timeout=4)
        self.assertEqual(rewards, {"next_cursor": "LTE="})
        self.assertIn("/rewards/markets/current", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"], {"sponsored": "true"})

        with patch(HTTP_REQUEST, return_value=FakeResponse({"data": []})) as mock_get:
            trades = clob_rest.get_builder_trades("0x" + "1" * 64, market="0xmarket")
        self.assertEqual(trades, {"data": []})
        self.assertIn("/builder/trades", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"]["builder_code"], "0x" + "1" * 64)

    def test_gamma_wrappers_cover_discovery_tags_profiles_and_sports(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"events": [{"id": "1"}]})) as mock_get:
            events = gamma.list_events_keyset(limit=999, after_cursor="next", closed=False)
        self.assertEqual(events, {"events": [{"id": "1"}]})
        self.assertIn("/events/keyset", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 500)
        self.assertEqual(mock_get.call_args.kwargs["params"]["after_cursor"], "next")
        self.assertFalse(mock_get.call_args.kwargs["params"]["closed"])

        with patch(HTTP_REQUEST, return_value=FakeResponse([{"slug": "politics"}])) as mock_get:
            related = gamma.get_tags_related_to_slug("election", status="active")
        self.assertEqual(related, [{"slug": "politics"}])
        self.assertIn("/tags/slug/election/related-tags/tags", request_url(mock_get))

        with patch(HTTP_REQUEST, return_value=FakeResponse({"marketTypes": ["moneyline"]})) as mock_get:
            market_types = gamma.get_sports_market_types()
        self.assertEqual(market_types, {"marketTypes": ["moneyline"]})
        self.assertIn("/sports/market-types", request_url(mock_get))

    def test_data_api_wrappers_cover_profile_market_and_builder_analytics(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse([{"value": 12}])) as mock_get:
            value = data_api.get_total_value("0xabc", market=["0xmarket"], timeout=7)
        self.assertEqual(value, [{"value": 12}])
        self.assertIn("/value", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"], {"user": "0xabc", "market": "0xmarket"})
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 7)

        with patch(HTTP_REQUEST, return_value=FakeResponse([{"token": "asset"}])) as mock_get:
            holders = data_api.get_top_holders(["0xmarket-a", "0xmarket-b"], limit=99, min_balance=-1)
        self.assertEqual(holders, [{"token": "asset"}])
        self.assertIn("/holders", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 20)
        self.assertEqual(mock_get.call_args.kwargs["params"]["minBalance"], 0)
        self.assertEqual(mock_get.call_args.kwargs["params"]["market"], "0xmarket-a,0xmarket-b")

        with patch(HTTP_REQUEST, return_value=FakeResponse([{"builder": "test"}])) as mock_get:
            builders = data_api.get_builder_leaderboard(time_period="all", limit=100)
        self.assertEqual(builders, [{"builder": "test"}])
        self.assertIn("/v1/builders/leaderboard", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["params"]["timePeriod"], "ALL")
        self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 50)

    def test_bridge_wrappers_cover_deposit_quote_status_and_withdrawal(self) -> None:
        with patch(HTTP_REQUEST, return_value=FakeResponse({"supportedAssets": []})) as mock_get:
            assets = bridge.get_supported_assets(timeout=3)
        self.assertEqual(assets, {"supportedAssets": []})
        self.assertIn("/supported-assets", request_url(mock_get))
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 3)

        with patch(HTTP_REQUEST, return_value=FakeResponse({"quoteId": "q"})) as mock_post:
            quote = bridge.get_quote(
                from_amount_base_unit="100",
                from_chain_id="137",
                from_token_address="0xfrom",
                recipient_address="0xrecipient",
                to_chain_id="137",
                to_token_address="0xto",
            )
        self.assertEqual(quote, {"quoteId": "q"})
        self.assertEqual(mock_post.call_args.args[0], "POST")
        self.assertIn("/quote", request_url(mock_post))
        self.assertEqual(mock_post.call_args.kwargs["json"]["fromAmountBaseUnit"], "100")

        with patch(HTTP_REQUEST, return_value=FakeResponse({"address": {"evm": "0xdep"}})) as mock_post:
            withdrawal = bridge.create_withdrawal_addresses(
                address="0xpoly",
                to_chain_id="1",
                to_token_address="0xtoken",
                recipient_addr="0xrecipient",
            )
        self.assertEqual(withdrawal["address"]["evm"], "0xdep")
        self.assertIn("/withdraw", request_url(mock_post))

    def test_relayer_and_clob_auth_wrappers_require_explicit_credentials(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "CLOB V2"):
            relayer.submit_transaction({"from": "0xabc"}, {})

        relayer_headers = {"RELAYER_API_KEY": "key", "RELAYER_API_KEY_ADDRESS": "0xabc"}
        with (
            patch(HTTP_REQUEST) as mock_post,
            self.assertRaisesRegex(RuntimeError, "CLOB V2"),
        ):
            relayer.submit_transaction({"from": "0xabc"}, relayer_headers)
        mock_post.assert_not_called()

        with self.assertRaises(ValueError):
            clob_auth.get_orders({})

        with patch(HTTP_REQUEST, return_value=FakeResponse({"data": []})) as mock_request:
            orders = clob_auth.get_orders(L2_HEADERS, market="0xmarket")
        self.assertEqual(orders, {"data": []})
        self.assertEqual(mock_request.call_args.args[:2], ("GET", "https://clob.polymarket.com/data/orders"))
        self.assertEqual(mock_request.call_args.kwargs["params"]["market"], "0xmarket")

    def test_polymarket_endpoint_registry_locks_documented_contract_caps(self) -> None:
        self.assertGreaterEqual(len(ALL_POLYMARKET_ENDPOINTS), 80)
        self.assertEqual(CLOB_ENDPOINTS["batch_prices_history"].max_items, 20)
        self.assertEqual(CLOB_ENDPOINTS["post_orders"].max_items, 15)
        self.assertEqual(CLOB_ENDPOINTS["cancel_orders"].max_items, 3000)
        self.assertEqual(CLOB_ENDPOINTS["post_orders"].auth, "l2")
        self.assertEqual(CLOB_ENDPOINTS["cancel_orders"].auth, "l2")
        self.assertTrue(all(endpoint.doc_url for endpoint in ALL_POLYMARKET_ENDPOINTS.values()))
        self.assertEqual(
            {endpoint.service for endpoint in ALL_POLYMARKET_ENDPOINTS.values()},
            {"gamma", "clob", "data", "bridge", "relayer"},
        )

    def test_documented_batch_caps_raise_instead_of_silently_truncating(self) -> None:
        with self.assertRaises(PolymarketValidationError):
            clob_rest.get_batch_price_history([str(i) for i in range(21)])

        with self.assertRaisesRegex(RuntimeError, "CLOB V2"):
            clob_auth.post_orders(({"order": i} for i in range(16)), L2_HEADERS)

        with self.assertRaisesRegex(RuntimeError, "CLOB V2"):
            clob_auth.cancel_orders((str(i) for i in range(3001)), L2_HEADERS)

    def test_shared_client_retries_transient_public_reads_and_raises_rate_limit(self) -> None:
        with (
            patch("polymarket.http_client.time.sleep") as mock_sleep,
            patch(
                HTTP_REQUEST,
                side_effect=[
                    FakeResponse({"error": "slow down"}, status_code=429, headers={"Retry-After": "0"}),
                    FakeResponse({"time": 123}),
                ],
            ) as mock_request,
        ):
            self.assertEqual(clob_rest.get_server_time(timeout=1), {"time": 123})

        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once()

    def test_shared_client_sets_identifying_json_headers_by_default(self) -> None:
        response = FakeResponse({"time": 123})
        with patch(HTTP_REQUEST, return_value=response) as mock_request:
            self.assertEqual(clob_rest.get_server_time(timeout=1), {"time": 123})

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], "market-sentinel/1.0")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertFalse(mock_request.call_args.kwargs["allow_redirects"])
        self.assertTrue(mock_request.call_args.kwargs["stream"])
        self.assertTrue(response.closed)

        with (
            patch("polymarket.http_client.time.sleep"),
            patch(
                HTTP_REQUEST,
                side_effect=[
                    FakeResponse({"error": "slow down"}, status_code=429, text="rate limited"),
                    FakeResponse({"error": "still slow"}, status_code=429, text="rate limited"),
                ],
            ),
        ):
            with self.assertRaises(PolymarketRateLimitError) as ctx:
                clob_rest.get_server_time(timeout=1)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_shared_client_rejects_redirects_private_urls_and_oversized_bodies(self) -> None:
        redirect = FakeResponse({}, status_code=302, headers={"Location": "https://example.test/next"})
        with patch(HTTP_REQUEST, return_value=redirect):
            with self.assertRaisesRegex(PolymarketHTTPError, "redirects are disabled"):
                clob_rest.get_server_time(timeout=1)
        self.assertTrue(redirect.closed)

        private_endpoint = PolymarketEndpoint("fixture", "GET", "/data", "https://127.0.0.1")
        with patch(HTTP_REQUEST) as request:
            with self.assertRaises(PolymarketValidationError):
                http_client.request_json(private_endpoint)
        request.assert_not_called()

        oversized = FakeResponse({}, content=b"123456789")
        with patch.object(http_client, "MAX_RESPONSE_BYTES", 8), patch(HTTP_REQUEST, return_value=oversized):
            with self.assertRaisesRegex(PolymarketHTTPError, "response limit"):
                clob_rest.get_server_time(timeout=1)
        self.assertTrue(oversized.closed)

    def test_clob_auth_readiness_distinguishes_sdk_l1_and_l2_auth(self) -> None:
        env = {
            "POLY_ADDRESS": "0xabc",
            "POLY_API_KEY": "key",
            "POLY_PASSPHRASE": "pass",
            "POLY_SIGNATURE": "sig",
            "POLY_TIMESTAMP": "1",
            "POLY_NONCE": "0",
        }
        readiness = build_clob_auth_readiness(
            {
                "private_key": "0x" + "1" * 64,
                "signature_type": 3,
                "funder_address": "0x" + "2" * 40,
            },
            environ=env,
        )

        self.assertTrue(readiness["ok"])
        self.assertTrue(readiness["sdk_trading_ready"])
        self.assertTrue(readiness["direct_l2_read_ready"])
        self.assertTrue(readiness["l1_rest_api_key_ready"])
        self.assertEqual(readiness["signature_type"]["name"], "POLY_1271")
        self.assertEqual(readiness["private_key"]["redacted"], "***")
        self.assertNotIn("1" * 64, str(readiness))

    def test_clob_auth_readiness_blocks_missing_required_funder_and_bad_key_shape(self) -> None:
        missing_funder = build_clob_auth_readiness(
            {"private_key": "0x" + "1" * 64, "signature_type": 3},
            environ={},
        )
        self.assertFalse(missing_funder["ok"])
        self.assertIn("requires an explicit funder", " ".join(missing_funder["blockers"]))

        bad_key = build_clob_auth_readiness(
            {"private_key": "not-a-key", "signature_type": 0},
            environ={},
        )
        self.assertFalse(bad_key["ok"])
        self.assertIn("0x-prefixed", " ".join(bad_key["blockers"]))

    def test_validate_sdk_trading_readiness_rejects_non_official_host_and_chain(self) -> None:
        with self.assertRaises(PolymarketValidationError):
            validate_sdk_trading_readiness(
                private_key="0x" + "1" * 64,
                signature_type=0,
                funder_address=None,
                chain_id=1,
            )

        with self.assertRaises(PolymarketValidationError):
            validate_sdk_trading_readiness(
                private_key="0x" + "1" * 64,
                signature_type=0,
                funder_address=None,
                host="https://example.invalid",
            )

    def test_live_order_cancel_harness_defaults_to_dry_run_and_redacts_credentials(self) -> None:
        plan = build_live_order_cancel_plan(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                api_key="explicit-api-key",
                api_secret="explicit-api-secret",
                api_passphrase="explicit-api-passphrase",
                cancel_immediately=True,
            )
        )

        self.assertEqual(plan["status"], "dry_run")
        self.assertFalse(plan["live_action"])
        self.assertEqual(plan["redacted_credentials"]["private_key"], "***")
        self.assertEqual(plan["redacted_credentials"]["explicit_api_credentials"], "***")
        self.assertNotIn("1" * 64, str(plan))
        self.assertNotIn("explicit-api-key", str(plan))
        self.assertNotIn("explicit-api-secret", str(plan))
        self.assertNotIn("explicit-api-passphrase", str(plan))
        self.assertFalse(plan["execution_supported"])
        self.assertIn("CLOB V2", " ".join(plan["transcript"]))

    def test_live_order_cancel_harness_fails_closed_before_any_transport_for_v2_migration(self) -> None:
        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=lambda _cfg: self.fail("legacy trader must never be created"),
            orderbook_getter=lambda _token: self.fail("orderbook preflight must not imply executable support"),
            geoblock_checker=lambda: self.fail("geoblock must not be reached"),
            recovery_writer=lambda _payload: self.fail("journal must not be touched"),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["live_action"])
        self.assertFalse(result["execution_supported"])
        self.assertIn("CLOB V2", " ".join(result["blockers"]))

    def test_live_order_cancel_harness_blocks_missing_allow_list_confirmation_and_caps(self) -> None:
        plan = build_live_order_cancel_plan(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.5",
                size="10",
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
            )
        )

        blockers = " ".join(plan["blockers"])
        self.assertEqual(plan["status"], "blocked")
        self.assertIn("Size 10 exceeds", blockers)
        self.assertIn("Missing token allow-list", blockers)
        self.assertIn("confirm-live-order-cancel", blockers)

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_executes_place_cancel_and_post_cancel_verification(self) -> None:
        placed_calls: list[dict[str, object]] = []
        journal_entries: list[dict[str, object]] = []
        trader_configs: list[TraderConfig] = []

        class FakeTrader(LiveHarnessTraderSupport):
            def __init__(self, cfg):
                self.calls = []
                trader_configs.append(cfg)

            def place_limit_order(self, **kwargs):
                self.calls.append(("place", kwargs))
                placed_calls.append(dict(kwargs))
                return {
                    "orderID": CLOB_ORDER_ID,
                    "status": "live",
                    "api_key": "placed-api-secret",
                    "message": "placed-generic-secret",
                }

            def cancel_order(self, order_id):
                self.calls.append(("cancel", order_id))
                return {
                    "canceled": [order_id],
                    "not_canceled": {},
                    "detail": "cancel-generic-secret",
                }

            def get_order(self, order_id):
                self.calls.append(("get", order_id))
                return {
                    "id": order_id,
                    "status": "ORDER_STATUS_CANCELED",
                    "size_matched": "0",
                    "associate_trades": [],
                    "message": "post-cancel-generic-secret",
                }

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                api_key="explicit-api-key",
                api_secret="explicit-api-secret",
                api_passphrase="explicit-api-passphrase",
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=FakeTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda payload: journal_entries.append(dict(payload)),
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["live_action"])
        self.assertTrue(result["audit"]["post_cancel_verified"])
        self.assertFalse(result["manual_reconciliation_required"])
        self.assertEqual(
            set(result["audit"]["placed"]),
            {"orderID", "order_id_present", "response_received"},
        )
        self.assertNotIn("api_key", result["audit"]["placed"])
        self.assertNotIn("placed-api-secret", str(result))
        self.assertNotIn("placed-generic-secret", str(result))
        self.assertNotIn("cancel-generic-secret", str(result))
        self.assertNotIn("post-cancel-generic-secret", str(result))
        self.assertTrue(placed_calls[0]["post_only"])
        self.assertEqual(placed_calls[0]["tif"], "GTC")
        self.assertEqual(trader_configs[0].api_key, "explicit-api-key")
        self.assertEqual(trader_configs[0].api_secret, "explicit-api-secret")
        self.assertEqual(trader_configs[0].api_passphrase, "explicit-api-passphrase")
        self.assertNotIn("explicit-api-secret", str(result))
        self.assertTrue(result["account_preflight"]["sufficient_allowance"])
        self.assertEqual(
            [entry["stage"] for entry in journal_entries],
            ["placement_pending", "order_placed_reconcile_required", "cancel_verified"],
        )
        self.assertTrue(result["audit"]["recovery_journal"]["resolved"])

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_does_not_capture_unsafe_order_identifiers(self) -> None:
        class UnsafeIdentifierTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def place_limit_order(self, **_kwargs):
                return {
                    "orderID": "unsafe order id with secret material",
                    "message": "generic-upstream-secret",
                    "nested": [{"detail": "nested-upstream-secret"}],
                }

            def cancel_order(self, _order_id):
                raise AssertionError("an unsafe order identifier must never be sent back to the venue")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=UnsafeIdentifierTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda _payload: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(result["audit"]["order_id"], "")
        self.assertEqual(
            result["audit"]["placed"],
            {"orderID": "", "order_id_present": False, "response_received": True},
        )
        self.assertNotIn("generic-upstream-secret", str(result))
        self.assertNotIn("nested-upstream-secret", str(result))
        self.assertNotIn("unsafe order id", str(result))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_preserves_order_identity_when_cancel_fails(self) -> None:
        class CancelFailureTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def place_limit_order(self, **_kwargs):
                return {"orderID": CLOB_ORDER_ID, "status": "live", "api_key": "secret"}

            def cancel_order(self, _order_id):
                raise RuntimeError("secret-bearing upstream error")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=CancelFailureTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda _payload: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(result["audit"]["order_id"], CLOB_ORDER_ID)
        self.assertEqual(result["audit"]["cancel_error"], {"type": "RuntimeError"})
        self.assertNotIn("api_key", result["audit"]["placed"])
        self.assertNotIn("secret-bearing", str(result))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_preserves_cancel_ack_when_post_read_fails(self) -> None:
        class PostReadFailureTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def place_limit_order(self, **_kwargs):
                return {"orderID": CLOB_ORDER_ID, "status": "live"}

            def cancel_order(self, order_id):
                return {"canceled": [order_id]}

            def get_order(self, _order_id):
                raise TimeoutError("upstream token must not be copied")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=PostReadFailureTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda _payload: None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(result["audit"]["order_id"], CLOB_ORDER_ID)
        self.assertEqual(result["audit"]["cancel"]["canceled"], [CLOB_ORDER_ID])
        self.assertTrue(result["audit"]["cancel"]["order_acknowledged"])
        self.assertEqual(result["audit"]["post_cancel_error"], {"type": "TimeoutError"})
        self.assertNotIn("upstream token", str(result))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_rejects_ambiguous_terminal_states_and_generic_ack(self) -> None:
        class AmbiguousTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg, cancel_payload, post_cancel_payload):
                self.cancel_payload = cancel_payload
                self.post_cancel_payload = post_cancel_payload

            def place_limit_order(self, **_kwargs):
                return {"orderID": CLOB_ORDER_ID, "status": "live"}

            def cancel_order(self, _order_id):
                return self.cancel_payload

            def get_order(self, _order_id):
                return self.post_cancel_payload

        cases = (
            ({"success": True}, {"id": CLOB_ORDER_ID, "status": "ORDER_STATUS_CANCELED"}),
            ({"canceled": [CLOB_ORDER_ID]}, {"id": CLOB_ORDER_ID, "status": "DONE", "open": False}),
            ({"canceled": [CLOB_ORDER_ID]}, {"id": OTHER_CLOB_ORDER_ID, "status": "ORDER_STATUS_CANCELED"}),
        )
        for cancel_response, post_cancel_response in cases:
            with self.subTest(cancel=cancel_response, post_cancel=post_cancel_response):
                result = run_live_order_cancel_verification(
                    LiveOrderCancelRequest(
                        token_id="token-1",
                        side="BUY",
                        price="0.01",
                        size="1",
                        allow_token_ids=["token-1"],
                        private_key="0x" + "1" * 64,
                        execute=True,
                        cancel_immediately=True,
                        confirmation=CONFIRM_LIVE_ORDER_CANCEL,
                    ),
                    trader_factory=lambda cfg, cancel=cancel_response, post=post_cancel_response: AmbiguousTrader(
                        cfg, cancel, post
                    ),
                    orderbook_getter=lambda _token_id: {
                        "bids": [{"price": "0.02"}],
                        "asks": [{"price": "0.04"}],
                    },
                    geoblock_checker=allowed_geoblock,
                    recovery_writer=lambda _payload: None,
                )

                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["audit"]["post_cancel_verified"])
                self.assertTrue(result["manual_reconciliation_required"])

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_blocks_market_taking_price_before_execution(self) -> None:
        class UnexpectedTrader:
            def __init__(self, _cfg):
                raise AssertionError("trader should not be created")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.04",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=UnexpectedTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda _payload: None,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("best ask", " ".join(result["blockers"]))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_blocks_one_base_unit_below_required_allowance(self) -> None:
        class InsufficientAllowanceTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def get_trading_balance_allowance(self, **_kwargs):
                return {"balance": "10000", "allowances": {"exchange": "9999"}}

            def place_limit_order(self, **_kwargs):
                raise AssertionError("placement must not run with insufficient allowance")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=InsufficientAllowanceTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda _payload: None,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["account_preflight"]["required_base_units"], 10000)
        self.assertTrue(result["account_preflight"]["sufficient_balance"])
        self.assertFalse(result["account_preflight"]["sufficient_allowance"])

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_blocks_funded_execution_without_recovery_writer(self) -> None:
        class UnexpectedTrader:
            def __init__(self, _cfg):
                raise AssertionError("trader must not be created without a recovery writer")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=UnexpectedTrader,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["live_action"])
        self.assertIn("recovery journal writer", " ".join(result["blockers"]))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_blocks_geographically_ineligible_execution(self) -> None:
        class UnexpectedTrader:
            def __init__(self, _cfg):
                raise AssertionError("trader must not be created when geoblocked")

        journal_entries: list[dict[str, object]] = []
        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=UnexpectedTrader,
            geoblock_checker=lambda: {"blocked": True, "country": "XX"},
            recovery_writer=lambda payload: journal_entries.append(dict(payload)),
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["live_action"])
        self.assertEqual(journal_entries, [])
        self.assertIn("blocked=false", " ".join(result["blockers"]))

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_rejects_partial_fill_after_cancel(self) -> None:
        class PartiallyFilledTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def place_limit_order(self, **_kwargs):
                return {"orderID": CLOB_ORDER_ID, "status": "live"}

            def cancel_order(self, order_id):
                return {"canceled": [order_id], "not_canceled": {}}

            def get_order(self, order_id):
                return {
                    "id": order_id,
                    "status": "ORDER_STATUS_CANCELED",
                    "size_matched": "0.5",
                    "associate_trades": ["trade-1"],
                }

        journal_entries: list[dict[str, object]] = []
        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=PartiallyFilledTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=lambda payload: journal_entries.append(dict(payload)),
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertFalse(result["audit"]["zero_fill_evidence"]["verified"])
        self.assertEqual(journal_entries[-1]["stage"], "cancel_incomplete")
        self.assertFalse(journal_entries[-1]["resolved"])

    @patch("polymarket.live_verification.POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_live_order_cancel_harness_fails_when_final_recovery_resolution_write_fails(self) -> None:
        class SuccessfulTrader(LiveHarnessTraderSupport):
            def __init__(self, _cfg):
                pass

            def place_limit_order(self, **_kwargs):
                return {"orderID": CLOB_ORDER_ID, "status": "live"}

            def cancel_order(self, order_id):
                return {"canceled": [order_id], "not_canceled": {}}

            def get_order(self, order_id):
                return {
                    "id": order_id,
                    "status": "ORDER_STATUS_CANCELED",
                    "size_matched": "0",
                    "associate_trades": [],
                }

        journal_entries: list[dict[str, object]] = []

        def write_recovery(payload):
            journal_entries.append(dict(payload))
            if payload["stage"] == "cancel_verified":
                raise OSError("durability failure")

        result = run_live_order_cancel_verification(
            LiveOrderCancelRequest(
                token_id="token-1",
                side="BUY",
                price="0.01",
                size="1",
                allow_token_ids=["token-1"],
                private_key="0x" + "1" * 64,
                execute=True,
                cancel_immediately=True,
                confirmation=CONFIRM_LIVE_ORDER_CANCEL,
            ),
            trader_factory=SuccessfulTrader,
            orderbook_getter=lambda _token_id: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
            geoblock_checker=allowed_geoblock,
            recovery_writer=write_recovery,
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertTrue(result["audit"]["post_cancel_verified"])
        self.assertEqual(result["audit"]["recovery_journal_error"], {"type": "OSError"})
        self.assertEqual(result["audit"]["recovery_journal"]["status"], "unresolved")
        self.assertFalse(result["audit"]["recovery_journal"]["resolved"])

    def test_live_validation_stage_gates_require_authenticated_read_before_funded(self) -> None:
        report = {
            "public_checks": {"clob_time": {"status": "ok"}},
            "authenticated_read_checks": {"clob_l2_orders": {"status": "blocked"}},
            "bridge_address_checks": {"deposit_address_creation": {"status": "skipped"}},
            "clob_auth_readiness": {"ok": True},
            "funded_live_order_check": {"status": "ready_to_execute", "live_action": True},
        }

        gates = build_live_validation_stage_gates(report)

        self.assertEqual(gates["credentialed_read_checks"], "blocked")
        self.assertFalse(gates["credentialed_read_ok"])
        self.assertFalse(gates["safe_to_attempt_funded_order"])
        self.assertIn("authenticated read", gates["next_step"])

        report["authenticated_read_checks"] = {
            "py_clob_client_credentials": {"status": "ok", "detail": "credentials derived"}
        }
        gates = build_live_validation_stage_gates(report)

        self.assertFalse(gates["credentialed_read_ok"])
        self.assertEqual(gates["accepted_credential_read_checks"], [])
        self.assertEqual(accepted_credential_read_checks(report["authenticated_read_checks"]), [])

        report["authenticated_read_checks"]["clob_l2_orders"] = {
            "status": "ok",
            "semantic_check": "authenticated_order_collection",
            "records_observed": 0,
        }
        gates = build_live_validation_stage_gates(report)

        self.assertTrue(gates["credentialed_read_ok"])
        self.assertFalse(gates["safe_to_attempt_funded_order"])
        self.assertIn("CLOB V2", gates["next_step"])
        self.assertEqual(gates["accepted_credential_read_checks"], ["clob_l2_orders"])

    def test_live_report_promotion_requires_concrete_authenticated_read_evidence(self) -> None:
        claimed_report = {
            "mode": "strict_cli",
            "stage_gates": {
                "credentialed_read_ok": True,
                "credentialed_read_checks": "ok",
                "funded_live_order_check": "blocked",
            },
            "authenticated_read_checks": {
                "py_clob_client_credentials": {"status": "ok", "detail": "credentials derived"},
            },
        }

        promotion = live_validation_report_promotion(claimed_report)

        self.assertEqual(promotion["credential_live_verified"], "blocked")
        self.assertFalse(promotion["can_promote_credential_live_verified"])
        self.assertIn("no accepted authenticated-read evidence", " ".join(promotion["blocked_reasons"]))

        verified_report = {
            "ok": True,
            "mode": "strict_cli",
            "source_provenance": clean_source_provenance(),
            "public_checks": successful_live_public_checks(),
            "authenticated_read_checks": accepted_live_authenticated_reads(),
            "funded_live_order_check": {"status": "blocked"},
        }
        verified = live_validation_report_summary(verified_report)

        self.assertEqual(verified["credential_live_verified"], "candidate_only")
        self.assertFalse(verified["verification_promotion"]["attested_workflow_verified"])
        self.assertTrue(verified["can_promote_credential_live_verified"])
        self.assertEqual(
            verified["verification_promotion"]["credential_evidence"][0]["check"],
            "clob_l2_orders",
        )

        dirty_report = json.loads(json.dumps(verified_report))
        dirty_report["source_provenance"].update(
            {"source_revision": "", "initial_clean": False, "final_clean": False, "stable": False}
        )
        dirty = live_validation_report_promotion(dirty_report)

        self.assertEqual(dirty["credential_live_verified"], "blocked")
        self.assertFalse(dirty["source_provenance_verified"])
        self.assertIn("clean initial/final source revisions", " ".join(dirty["blocked_reasons"]))

        invalid_public = json.loads(json.dumps(verified_report))
        invalid_public["public_checks"]["clob_time"]["semantic_check"] = "mere_http_200"
        self.assertFalse(
            live_validation_report_promotion(invalid_public)["can_promote_credential_live_verified"]
        )

        for field, bad_value in (
            ("semantic_check", "generic_collection"),
            ("records_observed", -1),
            ("records_observed", True),
        ):
            with self.subTest(credential_field=field, bad_value=bad_value):
                invalid_read = json.loads(json.dumps(verified_report))
                invalid_read["authenticated_read_checks"]["clob_l2_orders"][field] = bad_value
                self.assertFalse(
                    live_validation_report_promotion(invalid_read)["can_promote_credential_live_verified"]
                )

    @patch("polymarket.live_reports.POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True)
    def test_live_report_promotion_requires_funded_order_cancel_audit_evidence(self) -> None:
        dry_run_report = {
            "mode": "strict_cli",
            "funded_live_order_check": {
                "status": "dry_run",
                "live_action": False,
                "transcript": ["would place and cancel"],
            },
        }

        dry_run = live_validation_report_promotion(dry_run_report)

        self.assertEqual(dry_run["funded_live_verified"], "blocked")
        self.assertFalse(dry_run["can_promote_funded_live_verified"])

        funded_report = {
            "ok": True,
            "mode": "strict_cli",
            "source_provenance": clean_source_provenance(),
            "public_checks": successful_live_public_checks(),
            "authenticated_read_checks": accepted_live_authenticated_reads(),
            "funded_live_order_check": {
                "status": "ok",
                "live_action": True,
                "manual_reconciliation_required": False,
                **funded_live_safety_evidence(),
                "audit": {
                    "order_id": CLOB_ORDER_ID,
                    "placed": {"orderID": CLOB_ORDER_ID, "status": "live"},
                    "cancel": {"canceled": [CLOB_ORDER_ID]},
                    "post_cancel_order": {
                        "id": CLOB_ORDER_ID,
                        "status": "ORDER_STATUS_CANCELED",
                        "size_matched": "0",
                        "associate_trades": [],
                    },
                    "zero_fill_evidence": {
                        "verified": True,
                        "order_identity_matches": True,
                        "size_matched_zero": True,
                        "associated_trades_empty": True,
                    },
                    "recovery_journal": {
                        "status": "resolved",
                        "stage": "cancel_verified",
                        "resolved": True,
                    },
                    "post_cancel_verified": True,
                },
            },
        }
        funded = live_validation_report_summary(funded_report)

        self.assertEqual(funded["funded_live_verified"], "candidate_only")
        self.assertEqual(funded["verification_promotion"]["evidence_trust"], "local_unattested_candidate")
        self.assertTrue(funded["can_promote_funded_live_verified"])
        self.assertTrue(funded["verification_promotion"]["funded_evidence"])

        no_same_run_read = json.loads(json.dumps(funded_report))
        no_same_run_read.pop("authenticated_read_checks")
        rejected_missing_read = live_validation_report_promotion(no_same_run_read)
        self.assertEqual(rejected_missing_read["funded_live_verified"], "blocked")
        self.assertFalse(rejected_missing_read["can_promote_funded_live_verified"])
        self.assertIn("authenticated-read evidence", " ".join(rejected_missing_read["blocked_reasons"]))

        for cancel_payload, post_cancel_payload in (
            ({"success": True}, {"id": CLOB_ORDER_ID, "status": "ORDER_STATUS_CANCELED"}),
            ({"canceled": [CLOB_ORDER_ID]}, {"id": CLOB_ORDER_ID, "status": "DONE", "open": False}),
        ):
            with self.subTest(cancel=cancel_payload, post_cancel=post_cancel_payload):
                ambiguous = json.loads(json.dumps(funded_report))
                ambiguous["funded_live_order_check"]["audit"]["cancel"] = cancel_payload
                ambiguous["funded_live_order_check"]["audit"]["post_cancel_order"] = post_cancel_payload
                rejected = live_validation_report_promotion(ambiguous)
                self.assertEqual(rejected["funded_live_verified"], "blocked")
                self.assertFalse(rejected["can_promote_funded_live_verified"])

        safety_mutations = (
            (("account_authenticated_read_preflight", "same_trading_client"), False),
            (("account_authenticated_read_preflight", "account_identity_present"), False),
            (("account_preflight", "sufficient_balance"), False),
            (("account_preflight", "sufficient_allowance"), False),
            (("execution_guards", "post_only"), False),
            (("execution_guards", "time_in_force"), "FOK"),
            (("execution_guards", "maker_price_verified"), False),
            (("geoblock_preflight", "blocked"), True),
            (("source_revision_gate", "clean"), False),
            (("source_revision_gate", "matches_initial_revision"), False),
            (("source_revision_gate", "source_revision"), "b" * 40),
            (("source_revision_gate", "repository_origin"), "github.com/example/fork"),
            (("audit", "recovery_journal", "resolved"), False),
            (("audit", "recovery_journal", "stage"), "cancel_incomplete"),
            (("audit", "post_cancel_order", "size_matched"), "0.1"),
            (("audit", "post_cancel_order", "associate_trades"), ["trade-1"]),
            (("audit", "placed", "orderID"), OTHER_CLOB_ORDER_ID),
        )
        for path, bad_value in safety_mutations:
            with self.subTest(funded_field=".".join(path), bad_value=bad_value):
                invalid = json.loads(json.dumps(funded_report))
                target = invalid["funded_live_order_check"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = bad_value
                rejected = live_validation_report_promotion(invalid)
                self.assertEqual(rejected["funded_live_verified"], "blocked")
                self.assertFalse(rejected["can_promote_funded_live_verified"])

    def test_live_report_promotion_blocks_funded_candidate_until_v2_is_supported(self) -> None:
        report = json.loads(
            (LIVE_REPORT_FIXTURE_ROOT / "valid_funded_audit.json").read_text(encoding="utf-8")
        )

        promotion = live_validation_report_promotion(report)

        self.assertEqual(promotion["funded_live_verified"], "blocked")
        self.assertFalse(promotion["can_promote_funded_live_verified"])
        self.assertEqual(promotion["funded_evidence"], [])
        self.assertIn("CLOB V2", " ".join(promotion["blocked_reasons"]))

    def test_live_report_promotion_blocks_local_runbook_and_browser_smoke_reports(self) -> None:
        for mode in ("local_readiness_only", "credential_runbook_no_funded_actions", "browser_smoke", "browser_smoke_seed"):
            promotion = live_validation_report_promotion(
                {
                    "mode": mode,
                    "authenticated_read_checks": {"clob_l2_orders": {"status": "ok"}},
                    "funded_live_order_check": {
                        "status": "ok",
                        "live_action": True,
                        "audit": {
                            "order_id": "order-1",
                            "placed": {},
                            "cancel": {},
                            "post_cancel_order": {},
                            "post_cancel_verified": True,
                        },
                    },
                }
            )

            self.assertEqual(promotion["credential_live_verified"], "blocked")
            self.assertEqual(promotion["funded_live_verified"], "blocked")
            self.assertIn("local-only", " ".join(promotion["blocked_reasons"]))

    def test_live_report_promotion_inventory_keeps_static_coverage_unmutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            store_live_validation_report(
                {
                    "ok": True,
                    "mode": "strict_cli",
                    "source_provenance": clean_source_provenance(),
                    "public_checks": successful_live_public_checks(),
                    "authenticated_read_checks": accepted_live_authenticated_reads(),
                    "funded_live_order_check": {"status": "dry_run", "live_action": False},
                    "stage_gates": {
                        "credentialed_read_ok": True,
                        "credentialed_read_checks": "ok",
                        "funded_live_order_check": "dry_run",
                        "safe_to_attempt_funded_order": False,
                        "requires_explicit_live_approval": True,
                    },
                },
                source="cli",
                label="credential evidence",
                path=path,
            )
            store_live_validation_report(
                {
                    "mode": "browser_smoke",
                    "authenticated_read_checks": {"clob_l2_orders": {"status": "ok"}},
                    "funded_live_order_check": {
                        "status": "ok",
                        "live_action": True,
                        "audit": {
                            "order_id": "fake",
                            "placed": {},
                            "cancel": {},
                            "post_cancel_order": {},
                            "post_cancel_verified": True,
                        },
                    },
                    "stage_gates": {
                        "credentialed_read_ok": True,
                        "credentialed_read_checks": "ok",
                        "funded_live_order_check": "ok",
                        "safe_to_attempt_funded_order": False,
                        "requires_explicit_live_approval": True,
                    },
                },
                source="browser_smoke",
                label="local smoke",
                path=path,
            )

            inventory = live_validation_report_promotion_inventory(path=path)

        self.assertFalse(inventory["static_coverage_mutated"])
        self.assertEqual(inventory["credential_live_verified"], "candidate_only")
        self.assertFalse(inventory["attested_workflow_verified"])
        self.assertEqual(inventory["funded_live_verified"], "blocked")
        self.assertEqual(inventory["counts"]["credential_candidates"], 1)
        self.assertEqual(inventory["counts"]["funded_candidates"], 0)
        self.assertIn("credential evidence", inventory["credential_candidates"][0]["label"])

    def test_live_report_schema_accepts_deterministic_valid_fixtures(self) -> None:
        expected_modes = {
            "valid_credentialed_read.json": "strict_cli",
            "valid_funded_audit.json": "strict_cli",
            "valid_dry_run.json": "strict_cli",
            "valid_runbook.json": "credential_runbook_no_funded_actions",
            "valid_browser_smoke.json": "browser_smoke_seed",
        }
        for name, mode in expected_modes.items():
            payload = json.loads((LIVE_REPORT_FIXTURE_ROOT / name).read_text(encoding="utf-8"))
            validation = validate_live_validation_report(payload)

            self.assertTrue(validation["ok"], name)
            self.assertEqual(validation["mode"], mode)
            self.assertEqual(validation["schema_version"], 1)

        credentialed = live_validation_report_summary(
            json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        )
        funded = live_validation_report_summary(
            json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_funded_audit.json").read_text(encoding="utf-8"))
        )
        dry_run = live_validation_report_summary(
            json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        )
        runbook = live_validation_report_summary(
            json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_runbook.json").read_text(encoding="utf-8"))
        )
        browser = live_validation_report_summary(
            json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_browser_smoke.json").read_text(encoding="utf-8"))
        )

        self.assertEqual(credentialed["credential_live_verified"], "candidate_only")
        self.assertEqual(funded["funded_live_verified"], "blocked")
        self.assertEqual(dry_run["funded_live_verified"], "blocked")
        self.assertEqual(runbook["credential_live_verified"], "blocked")
        self.assertEqual(browser["credential_live_verified"], "blocked")

    def test_live_report_schema_rejects_invalid_fixtures_and_bad_json(self) -> None:
        for name in ("invalid_missing_mode.json", "invalid_bad_stage_gates.json"):
            payload = json.loads((LIVE_REPORT_FIXTURE_ROOT / name).read_text(encoding="utf-8"))
            validation = validate_live_validation_report(payload)

            self.assertFalse(validation["ok"], name)
            self.assertTrue(validation["errors"], name)
            with self.assertRaises(LiveValidationReportSchemaError):
                ensure_live_validation_report_valid(payload)

        with self.assertRaises(LiveValidationReportSchemaError) as ctx:
            parse_live_validation_report_json("[]")
        self.assertFalse(ctx.exception.validation["ok"])
        self.assertIn("decode to an object", " ".join(ctx.exception.validation["errors"]))

        with self.assertRaises(LiveValidationReportSchemaError) as bad_json:
            parse_live_validation_report_json("{not-json")
        self.assertFalse(bad_json.exception.validation["ok"])
        self.assertIn("valid JSON", " ".join(bad_json.exception.validation["errors"]))

    def test_live_report_store_attaches_schema_validation_and_rejects_invalid_reports(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        invalid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "invalid_bad_stage_gates.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            stored = store_live_validation_report(valid, source="fixture", label="valid credentialed", path=path)

            self.assertTrue(stored["schema_validation"]["ok"])
            self.assertEqual(stored["schema_validation"]["mode"], "strict_cli")

            with self.assertRaises(LiveValidationReportSchemaError) as ctx:
                store_live_validation_report(invalid, source="fixture", label="bad", path=path)
            self.assertFalse(ctx.exception.validation["ok"])
            self.assertIn("stage_gates must be an object", " ".join(ctx.exception.validation["errors"]))

    def test_live_report_store_hashes_provenance_and_skips_duplicates_by_default(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        valid["api_key"] = "redacted-hash-secret"
        same_redacted_payload = json.loads(json.dumps(valid))
        same_redacted_payload["api_key"] = "different-secret-same-redacted-payload"

        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            path = temp / "reports.json"
            source_file = temp / "credentialed.json"
            first = store_live_validation_report(
                valid,
                source="fixture",
                label="credentialed",
                path=path,
                source_file=source_file,
            )

            expected_hash = live_validation_report_payload_hash(valid)
            self.assertEqual(len(expected_hash), 64)
            self.assertEqual(expected_hash, live_validation_report_payload_hash(same_redacted_payload))
            self.assertEqual(first["payload_hash"], expected_hash)
            self.assertEqual(first["provenance"]["source_file_name"], "credentialed.json")
            duplicate_lookup = find_live_validation_report_duplicate(expected_hash, path=path)
            self.assertIsNotNone(duplicate_lookup)
            self.assertEqual(duplicate_lookup["key"], first["key"])

            skipped = store_live_validation_report(
                same_redacted_payload,
                source="fixture_replay",
                label="credentialed replay",
                path=path,
                source_file=temp / "credentialed-copy.json",
            )

            self.assertFalse(skipped["stored"])
            self.assertTrue(skipped["duplicate"])
            self.assertEqual(skipped["duplicate_key"], first["key"])
            self.assertEqual(skipped["duplicate_policy"], "skip")
            self.assertEqual(skipped["duplicate_audit_event"]["source_file_name"], "credentialed-copy.json")
            listing = list_live_validation_reports(path=path)
            self.assertEqual(listing["counts"]["entries"], 1)
            self.assertEqual(listing["counts"]["duplicate_imports"], 1)
            self.assertEqual(listing["entries"][0]["duplicate_import_count"], 1)
            self.assertTrue(listing["entries"][0]["duplicate"])

            allowed = store_live_validation_report(
                same_redacted_payload,
                source="fixture_replay",
                label="credentialed allowed duplicate",
                path=path,
                source_file=temp / "credentialed-allowed.json",
                allow_duplicate=True,
            )

            self.assertTrue(allowed["stored"])
            self.assertTrue(allowed["duplicate"])
            self.assertEqual(allowed["duplicate_of"], first["key"])
            self.assertEqual(allowed["payload_hash"], expected_hash)
            listing = list_live_validation_reports(path=path)
            self.assertEqual(listing["counts"]["entries"], 2)
            self.assertEqual(listing["counts"]["duplicate_payloads"], 1)
            self.assertEqual({entry["payload_hash"] for entry in listing["entries"]}, {expected_hash})
            disk = path.read_text(encoding="utf-8")
            self.assertNotIn("redacted-hash-secret", disk)
            self.assertNotIn("different-secret-same-redacted-payload", disk)

    def test_live_report_review_bundle_is_sanitized_and_maps_promotion_to_coverage(self) -> None:
        report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))
        report["api_key"] = "review-secret-api-key"
        report["operator_commands"] = {
            "safe_live_probe": "python scripts/verify_polymarket_live.py --timeout 8",
            "credentialed_read": "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --report-file live-report.json",
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reports.json"
            stored = store_live_validation_report(
                report,
                source="fixture",
                label="review dry run",
                path=path,
                source_file="valid_dry_run.json",
            )
            store_live_validation_report(
                report,
                source="fixture",
                label="review dry run duplicate",
                path=path,
                source_file="valid_dry_run-copy.json",
            )

            bundle = live_validation_report_review_bundle(stored["key"], path=path)

        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.assertEqual(bundle["source"], "polymarket_live_validation_report_review_bundle")
        self.assertFalse(bundle["static_coverage_mutated"])
        self.assertFalse(bundle["funded_execution_exposed"])
        self.assertEqual(bundle["report"]["payload_hash"], stored["payload_hash"])
        self.assertEqual(bundle["report"]["provenance"]["source_file_name"], "valid_dry_run.json")
        self.assertTrue(bundle["schema_validation"]["ok"])
        self.assertEqual(bundle["duplicate_history"]["duplicate_import_count"], 1)
        self.assertEqual(bundle["duplicate_history"]["duplicate_imports"][0]["source_file_name"], "valid_dry_run-copy.json")
        self.assertEqual(
            bundle["operator_commands"]["credentialed_read"],
            "python scripts/verify_polymarket_live.py --require-authenticated-read-ok --report-file live-report.json",
        )
        self.assertEqual(bundle["promotion_review"]["funded_live_verified"], "blocked")
        self.assertIn("Funded live verification requires", " ".join(bundle["promotion_review"]["blocked_reasons"]))
        self.assertFalse(bundle["coverage_tier_mapping"]["levels"]["funded_live_verified"]["can_promote_from_report"])
        self.assertTrue(bundle["coverage_tier_mapping"]["levels"]["credential_live_verified"]["can_promote_from_report"])
        self.assertEqual(
            bundle["coverage_tier_mapping"]["levels"]["credential_live_verified"]["review_effect"],
            "candidate_evidence_only",
        )
        self.assertNotIn("payload", bundle)
        bundle_text = json.dumps(bundle, sort_keys=True)
        self.assertNotIn("review-secret-api-key", bundle_text)
        markdown = live_validation_report_review_markdown(bundle)
        self.assertIn("Polymarket Live Validation Review Bundle", markdown)
        self.assertIn("Static coverage mutated: false", markdown)
        self.assertIn("python scripts/verify_polymarket_live.py --timeout 8", markdown)
        self.assertNotIn("review-secret-api-key", markdown)

    def test_live_report_decision_ledger_requires_matching_review_hash_and_exports(self) -> None:
        report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        report["api_key"] = "decision-secret-api-key"
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            report_path = temp / "reports.json"
            decision_path = temp / "decisions.json"
            stored = store_live_validation_report(
                report,
                source="fixture",
                label="credential decision",
                path=report_path,
                source_file="valid_credentialed_read.json",
            )
            bundle = live_validation_report_review_bundle(stored["key"], path=report_path)
            self.assertIsNotNone(bundle)
            assert bundle is not None
            review_hash = live_validation_report_review_bundle_hash(bundle)
            self.assertEqual(bundle["review_bundle_hash"], review_hash)

            accepted = record_live_validation_report_decision(
                report_key=stored["key"],
                payload_hash=stored["payload_hash"],
                target_tier="credential_live_verified",
                decision="accepted",
                reviewer_note="Authenticated read evidence is present in the stored review bundle.",
                review_bundle_hash=review_hash,
                reviewer="unit-test",
                report_store_path=report_path,
                decision_path=decision_path,
            )

            self.assertTrue(accepted["stored"])
            self.assertEqual(accepted["decision"], "accepted")
            self.assertEqual(accepted["target_tier"], "credential_live_verified")
            self.assertTrue(accepted["review_bundle_hash_verified"])
            self.assertFalse(accepted["static_coverage_mutated"])
            self.assertEqual(accepted["promotion_effect"], "ledger_only_no_static_coverage_mutation")

            rejected = record_live_validation_report_decision(
                report_key=stored["key"],
                payload_hash=stored["payload_hash"],
                target_tier="funded_live_verified",
                decision="rejected",
                reviewer_note="Funded order/cancel evidence is absent.",
                review_bundle_hash=review_hash,
                reviewer="unit-test",
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(rejected["decision"], "rejected")

            with self.assertRaises(ValueError) as payload_mismatch:
                record_live_validation_report_decision(
                    report_key=stored["key"],
                    payload_hash="bad-payload-hash",
                    target_tier="credential_live_verified",
                    decision="accepted",
                    reviewer_note="bad",
                    review_bundle_hash=review_hash,
                    report_store_path=report_path,
                    decision_path=decision_path,
                )
            self.assertIn("payload_hash mismatch", str(payload_mismatch.exception))

            with self.assertRaises(ValueError) as tamper_mismatch:
                record_live_validation_report_decision(
                    report_key=stored["key"],
                    payload_hash=stored["payload_hash"],
                    target_tier="credential_live_verified",
                    decision="accepted",
                    reviewer_note="bad",
                    review_bundle_hash="bad-review-hash",
                    report_store_path=report_path,
                    decision_path=decision_path,
                )
            self.assertIn("review_bundle_hash mismatch", str(tamper_mismatch.exception))

            with self.assertRaises(ValueError) as blocked_accept:
                record_live_validation_report_decision(
                    report_key=stored["key"],
                    payload_hash=stored["payload_hash"],
                    target_tier="funded_live_verified",
                    decision="accepted",
                    reviewer_note="bad",
                    review_bundle_hash=review_hash,
                    report_store_path=report_path,
                    decision_path=decision_path,
                )
            self.assertIn("Cannot accept funded_live_verified", str(blocked_accept.exception))

            ledger = list_live_validation_report_decisions(path=decision_path)
            self.assertEqual(ledger["counts"]["entries"], 2)
            self.assertEqual(ledger["counts"]["accepted"], 1)
            self.assertEqual(ledger["counts"]["rejected"], 1)
            markdown = live_validation_report_decisions_markdown(ledger)
            self.assertIn("Promotion Decision Ledger", markdown)
            self.assertIn("static coverage tiers", markdown)
            self.assertNotIn("decision-secret-api-key", json.dumps(ledger, sort_keys=True))

            cli = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "review_polymarket_live_decisions.py"),
                    "--export-ledger",
                    "--markdown",
                    "--decision-path",
                    str(decision_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cli.returncode, 0)
            self.assertIn("Promotion Decision Ledger", cli.stdout)
            self.assertNotIn("decision-secret-api-key", cli.stdout)

    def test_live_report_promotion_proposal_exports_candidates_and_detects_stale_decisions(self) -> None:
        report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        report["api_key"] = "proposal-secret-api-key"
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            report_path = temp / "reports.json"
            decision_path = temp / "decisions.json"
            stored = store_live_validation_report(
                report,
                source="fixture",
                label="credential proposal",
                path=report_path,
                source_file="valid_credentialed_read.json",
            )
            bundle = live_validation_report_review_bundle(stored["key"], path=report_path)
            self.assertIsNotNone(bundle)
            assert bundle is not None
            review_hash = live_validation_report_review_bundle_hash(bundle)
            record_live_validation_report_decision(
                report_key=stored["key"],
                payload_hash=stored["payload_hash"],
                target_tier="credential_live_verified",
                decision="accepted",
                reviewer_note="Authenticated read evidence is accepted for proposal generation.",
                review_bundle_hash=review_hash,
                reviewer="unit-test",
                report_store_path=report_path,
                decision_path=decision_path,
            )

            proposal = live_validation_coverage_promotion_proposal(
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(proposal["source"], "polymarket_live_validation_coverage_promotion_proposal")
            self.assertTrue(proposal["human_review_required"])
            self.assertFalse(proposal["automerge_enabled"])
            self.assertFalse(proposal["apply_by_default"])
            self.assertFalse(proposal["static_coverage_mutated"])
            self.assertEqual(proposal["counts"]["accepted_candidates"], 1)
            self.assertEqual(proposal["counts"]["stale_decisions"], 0)
            self.assertGreaterEqual(proposal["counts"]["proposed_changes"], 4)
            self.assertEqual(proposal["proposal_hash"], live_validation_coverage_promotion_proposal_hash(proposal))
            self.assertIn("polymarket/coverage.py", proposal["patch_proposal"]["files"])
            markdown = live_validation_coverage_promotion_proposal_markdown(proposal)
            self.assertIn("Coverage Promotion Proposal", markdown)
            self.assertIn("Automerge enabled: false", markdown)
            self.assertNotIn("proposal-secret-api-key", json.dumps(proposal, sort_keys=True))
            self.assertNotIn("proposal-secret-api-key", markdown)

            cli = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "review_polymarket_live_decisions.py"),
                    "--export-proposal",
                    "--markdown",
                    "--report-store-path",
                    str(report_path),
                    "--decision-path",
                    str(decision_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cli.returncode, 0)
            self.assertIn("Coverage Promotion Proposal", cli.stdout)
            self.assertNotIn("proposal-secret-api-key", cli.stdout)

            report_store = json.loads(report_path.read_text(encoding="utf-8"))
            report_store["reports"][stored["key"]]["payload_hash"] = "stale-payload-hash"
            report_path.write_text(json.dumps(report_store), encoding="utf-8")
            stale = live_validation_coverage_promotion_proposal(
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(stale["counts"]["accepted_candidates"], 0)
            self.assertEqual(stale["counts"]["stale_decisions"], 1)
            stale_reasons = stale["stale_decisions"][0]["stale_reasons"]
            self.assertIn("payload_hash_mismatch", stale_reasons)
            self.assertIn("review_bundle_hash_mismatch", stale_reasons)

    def test_live_report_promotion_proposal_snapshot_archive_detects_stale_and_prunes(self) -> None:
        report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        report["api_key"] = "snapshot-secret-api-key"
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            report_path = temp / "reports.json"
            decision_path = temp / "decisions.json"
            snapshot_path = temp / "proposal-snapshots.json"
            stored = store_live_validation_report(
                report,
                source="fixture",
                label="snapshot proposal",
                path=report_path,
                source_file="valid_credentialed_read.json",
            )
            bundle = live_validation_report_review_bundle(stored["key"], path=report_path)
            self.assertIsNotNone(bundle)
            assert bundle is not None
            record_live_validation_report_decision(
                report_key=stored["key"],
                payload_hash=stored["payload_hash"],
                target_tier="credential_live_verified",
                decision="accepted",
                reviewer_note="Authenticated read evidence is accepted for snapshot storage.",
                review_bundle_hash=str(bundle["review_bundle_hash"]),
                reviewer="unit-test",
                report_store_path=report_path,
                decision_path=decision_path,
            )
            proposal = live_validation_coverage_promotion_proposal(
                report_store_path=report_path,
                decision_path=decision_path,
                target_tier="credential_live_verified",
            )
            collision_snapshot_path = temp / "collision-proposal-snapshots.json"
            with (
                patch("polymarket.live_reports._now", return_value=1700000000),
                patch("polymarket.live_reports.time.time_ns", return_value=1700000000000000000),
            ):
                first_collision = store_live_validation_coverage_promotion_proposal_snapshot(
                    proposal=proposal,
                    report_store_path=report_path,
                    decision_path=decision_path,
                    target_tier="credential_live_verified",
                    path=collision_snapshot_path,
                    source="unit-test",
                    label="same clock snapshot",
                )
                second_collision = store_live_validation_coverage_promotion_proposal_snapshot(
                    proposal=proposal,
                    report_store_path=report_path,
                    decision_path=decision_path,
                    target_tier="credential_live_verified",
                    path=collision_snapshot_path,
                    source="unit-test",
                    label="same clock snapshot",
                )
            self.assertNotEqual(first_collision["key"], second_collision["key"])
            collision_listing = list_live_validation_coverage_promotion_proposal_snapshots(
                path=collision_snapshot_path,
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(collision_listing["counts"]["entries"], 2)

            snapshot = store_live_validation_coverage_promotion_proposal_snapshot(
                proposal=proposal,
                report_store_path=report_path,
                decision_path=decision_path,
                target_tier="credential_live_verified",
                path=snapshot_path,
                source="unit-test",
                label="credential proposal snapshot",
            )
            self.assertTrue(snapshot["stored"])
            self.assertFalse(snapshot["static_coverage_mutated"])
            self.assertEqual(snapshot["snapshot_status"], "current")

            opened = load_live_validation_coverage_promotion_proposal_snapshot(
                snapshot["key"],
                path=snapshot_path,
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertIsNotNone(opened)
            assert opened is not None
            self.assertEqual(opened["entry"]["snapshot_status"], "current")
            self.assertNotIn("snapshot-secret-api-key", json.dumps(opened, sort_keys=True))
            markdown = live_validation_promotion_proposal_snapshot_markdown(opened)
            self.assertIn("Promotion Proposal Snapshot", markdown)
            self.assertIn("Static coverage mutated: false", markdown)
            self.assertNotIn("snapshot-secret-api-key", markdown)

            duplicate = store_live_validation_report(
                report,
                source="fixture",
                label="snapshot proposal changed",
                path=report_path,
                source_file="valid_credentialed_read.json",
                allow_duplicate=True,
            )
            changed_bundle = live_validation_report_review_bundle(duplicate["key"], path=report_path)
            self.assertIsNotNone(changed_bundle)
            assert changed_bundle is not None
            record_live_validation_report_decision(
                report_key=duplicate["key"],
                payload_hash=duplicate["payload_hash"],
                target_tier="credential_live_verified",
                decision="accepted",
                reviewer_note="Second accepted evidence changes the proposal hash.",
                review_bundle_hash=str(changed_bundle["review_bundle_hash"]),
                reviewer="unit-test",
                report_store_path=report_path,
                decision_path=decision_path,
            )
            stale_listing = list_live_validation_coverage_promotion_proposal_snapshots(
                path=snapshot_path,
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(stale_listing["counts"]["stale"], 1)
            self.assertIn("proposal_hash_mismatch", stale_listing["entries"][0]["stale_reasons"])
            stale_opened = load_live_validation_coverage_promotion_proposal_snapshot(
                snapshot["key"],
                path=snapshot_path,
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertIsNotNone(stale_opened)
            assert stale_opened is not None
            diff = stale_opened["diff"]
            self.assertTrue(diff["changed"])
            self.assertIn("proposal_hash", diff["change_categories"])
            self.assertEqual(len(diff["accepted_decisions"]["added"]), 1)
            self.assertEqual(diff["accepted_decisions"]["added"][0]["report_key"], duplicate["key"])
            self.assertEqual(diff["accepted_decisions"]["added"][0]["target_tier"], "credential_live_verified")
            self.assertIn("Current-vs-Snapshot Diff", live_validation_promotion_proposal_snapshot_markdown(stale_opened))
            diff_markdown = live_validation_promotion_proposal_snapshot_diff_markdown(diff)
            self.assertIn("Current-vs-Snapshot Diff", diff_markdown)
            self.assertNotIn("snapshot-secret-api-key", json.dumps(diff, sort_keys=True))
            self.assertNotIn("snapshot-secret-api-key", diff_markdown)

            changed = live_validation_coverage_promotion_proposal(
                report_store_path=report_path,
                decision_path=decision_path,
                target_tier="credential_live_verified",
            )
            second = store_live_validation_coverage_promotion_proposal_snapshot(
                proposal=changed,
                report_store_path=report_path,
                decision_path=decision_path,
                target_tier="credential_live_verified",
                path=snapshot_path,
                source="unit-test",
                label="retained snapshot",
                max_entries=1,
            )
            pruned = list_live_validation_coverage_promotion_proposal_snapshots(
                path=snapshot_path,
                report_store_path=report_path,
                decision_path=decision_path,
            )
            self.assertEqual(pruned["counts"]["entries"], 1)
            self.assertEqual(pruned["entries"][0]["key"], second["key"])

            purged = purge_live_validation_coverage_promotion_proposal_snapshots(keys=[second["key"]], path=snapshot_path)
            self.assertEqual(purged["deleted"], 1)
            self.assertEqual(purged["counts"]["entries"], 0)

    def test_live_report_replay_validates_valid_and_invalid_fixtures_without_import(self) -> None:
        result = replay_live_validation_report_paths(
            [
                LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json",
                LIVE_REPORT_FIXTURE_ROOT / "valid_funded_audit.json",
                LIVE_REPORT_FIXTURE_ROOT / "invalid_missing_mode.json",
            ]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["funded_execution_exposed"])
        self.assertEqual(result["counts"]["files"], 3)
        self.assertEqual(result["counts"]["valid"], 2)
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["counts"]["imported"], 0)
        credentialed = result["entries"][0]
        funded = result["entries"][1]
        invalid = result["entries"][2]
        self.assertEqual(credentialed["summary"]["credential_live_verified"], "candidate_only")
        self.assertEqual(funded["summary"]["funded_live_verified"], "blocked")
        self.assertFalse(invalid["schema_validation"]["ok"])
        self.assertIn("non-empty string mode", " ".join(invalid["schema_validation"]["errors"]))
        for entry in result["entries"]:
            self.assertNotIn("payload", entry)

    def test_live_report_replay_imports_only_valid_reports_redacted(self) -> None:
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json").read_text(encoding="utf-8"))
        valid["api_key"] = "replay-secret-api-key"
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            report_path = temp / "credentialed.json"
            report_path.write_text(json.dumps(valid), encoding="utf-8")
            store_path = temp / "store.json"
            result = replay_live_validation_report_paths(
                [report_path, LIVE_REPORT_FIXTURE_ROOT / "invalid_bad_stage_gates.json"],
                import_reports=True,
                store_path=store_path,
                label_prefix="replay",
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["mode"], "import")
            self.assertEqual(result["counts"]["valid"], 1)
            self.assertEqual(result["counts"]["invalid"], 1)
            self.assertEqual(result["counts"]["imported"], 1)
            self.assertTrue(result["entries"][0]["imported"])
            self.assertEqual(result["entries"][0]["stored"]["label"], "replay credentialed")
            self.assertEqual(result["entries"][0]["stored"]["provenance"]["source_file_name"], "credentialed.json")
            self.assertEqual(len(result["entries"][0]["payload_hash"]), 64)
            self.assertFalse(result["entries"][1]["imported"])
            disk = store_path.read_text(encoding="utf-8")
            self.assertNotIn("replay-secret-api-key", disk)
            self.assertIn("***", disk)
            listing = list_live_validation_reports(path=store_path)
            self.assertEqual(listing["counts"]["entries"], 1)

    def test_live_report_replay_detects_and_skips_duplicate_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            store_path = temp / "store.json"
            duplicate_result = replay_live_validation_report_paths(
                [
                    LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json",
                    LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json",
                ],
                import_reports=True,
                store_path=store_path,
            )

            self.assertTrue(duplicate_result["ok"])
            self.assertEqual(duplicate_result["counts"]["valid"], 2)
            self.assertEqual(duplicate_result["counts"]["imported"], 1)
            self.assertEqual(duplicate_result["counts"]["duplicates"], 1)
            self.assertEqual(duplicate_result["counts"]["skipped_duplicates"], 1)
            self.assertTrue(duplicate_result["entries"][1]["duplicate"])
            self.assertTrue(duplicate_result["entries"][1]["duplicate_skipped"])
            listing = list_live_validation_reports(path=store_path)
            self.assertEqual(listing["counts"]["entries"], 1)
            self.assertEqual(listing["counts"]["duplicate_imports"], 1)

            allow_store_path = temp / "allow-store.json"
            with patch("polymarket.live_reports.time.time_ns", return_value=1234567890):
                allowed_result = replay_live_validation_report_paths(
                    [
                        LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json",
                        LIVE_REPORT_FIXTURE_ROOT / "valid_credentialed_read.json",
                    ],
                    import_reports=True,
                    store_path=allow_store_path,
                    allow_duplicate=True,
                )

            self.assertTrue(allowed_result["ok"])
            self.assertEqual(allowed_result["counts"]["imported"], 2)
            self.assertEqual(allowed_result["counts"]["duplicates"], 1)
            self.assertEqual(allowed_result["counts"]["skipped_duplicates"], 0)
            allow_listing = list_live_validation_reports(path=allow_store_path)
            self.assertEqual(allow_listing["counts"]["entries"], 2)
            self.assertEqual(allow_listing["counts"]["duplicate_payloads"], 1)
            self.assertEqual(len({entry["key"] for entry in allow_listing["entries"]}), 2)

    def test_live_report_replay_cli_outputs_structured_json_and_nonzero_for_invalid(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "replay_polymarket_live_reports.py"),
                "--json",
                str(LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json"),
                str(LIVE_REPORT_FIXTURE_ROOT / "invalid_missing_mode.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["counts"]["valid"], 1)
        self.assertEqual(payload["counts"]["invalid"], 1)
        self.assertEqual(payload["entries"][0]["summary"]["funded_live_verified"], "blocked")
        self.assertIn("strict_cli", payload["entries"][1]["schema_validation"]["accepted_modes"])

    def test_credential_runbook_inventories_env_and_never_exposes_funded_actions(self) -> None:
        env = {
            "POLY_ADDRESS": "0x" + "a" * 40,
            "POLY_API_KEY": "api-key-secret",
            "POLY_PASSPHRASE": "passphrase-secret",
            "POLY_SIGNATURE": "signature-secret",
            "POLY_TIMESTAMP": "123",
            "POLY_API_SECRET": "websocket-secret",
            "RELAYER_API_KEY": "relayer-secret",
            "RELAYER_API_KEY_ADDRESS": "0x" + "b" * 40,
            "PRIVATE_KEY": "0x" + "1" * 64,
            "SIGNATURE_TYPE": "0",
        }

        runbook = build_polymarket_credential_runbook(environ=env)

        self.assertEqual(runbook["mode"], "credential_runbook_no_funded_actions")
        self.assertEqual(runbook["network_calls"], "none")
        self.assertFalse(runbook["funded_execution_exposed"])
        self.assertFalse(runbook["safe_to_attempt_funded_order"])
        self.assertEqual(runbook["readiness"]["direct_l2_read_headers"]["status"], "ok")
        self.assertEqual(runbook["readiness"]["user_websocket_auth_payload"]["status"], "ok")
        self.assertEqual(runbook["readiness"]["relayer_headers"]["status"], "ok")
        self.assertIn("clob_l2_orders", runbook["readiness"]["credentialed_read_candidates"])
        self.assertIn("verify_polymarket_credentials.py --json", runbook["operator_commands"]["credential_inventory"])
        self.assertIn("--require-authenticated-read-ok", runbook["operator_commands"]["credentialed_read_no_funded_actions"])
        self.assertIn("--allow-funded-order", runbook["operator_commands"]["funded_order_cancel_requires_approval"])
        self.assertIn(CONFIRM_LIVE_ORDER_CANCEL, runbook["operator_commands"]["funded_order_cancel_requires_approval"])
        self.assertNotIn("api-key-secret", str(runbook))
        self.assertNotIn("websocket-secret", str(runbook))
        self.assertNotIn("1" * 64, str(runbook))

    def test_credential_runbook_blocks_missing_authenticated_read_inputs(self) -> None:
        runbook = build_polymarket_credential_runbook(environ={})

        self.assertFalse(runbook["readiness"]["non_destructive_auth_ready"])
        self.assertEqual(runbook["readiness"]["direct_l2_read_headers"]["status"], "blocked")
        self.assertEqual(runbook["readiness"]["user_websocket_auth_payload"]["status"], "blocked")
        self.assertIn("POLY_API_KEY", runbook["env_inventory"]["user_websocket_auth"]["requirements"][0]["missing"])
        self.assertIn("Do not attempt funded verification", " ".join(runbook["next_steps"]))

    def test_websocket_subscription_builders_cover_market_user_and_sports_channels(self) -> None:
        self.assertEqual(
            build_market_subscription(["asset-1"], custom_feature_enabled=True),
            {"assets_ids": ["asset-1"], "type": "market", "custom_feature_enabled": True},
        )
        self.assertEqual(
            build_user_subscription(
                {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
                ["0xcondition"],
            ),
            {
                "auth": {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
                "type": "user",
                "markets": ["0xcondition"],
            },
        )
        with self.assertRaises(ValueError):
            build_user_subscription({"apiKey": "key"})
        self.assertEqual(sports_ws_url(), "wss://sports-api.polymarket.com/ws")

    def test_user_websocket_probe_sends_subscription_and_redacts_result(self) -> None:
        call_order = []

        class FakeConnection:
            def __init__(self) -> None:
                self.sent = []
                self.closed = False

            def getstatus(self) -> int:
                call_order.append("status")
                return 101

            def settimeout(self, timeout: float) -> None:
                self.read_timeout = timeout

            def send(self, message: str) -> None:
                call_order.append("send")
                self.sent.append(message)

            def recv(self) -> str:
                return "PONG"

            def close(self) -> None:
                self.closed = True

        connections = []

        def factory(url, *, timeout, redirect_limit):
            conn = FakeConnection()
            connections.append((url, timeout, redirect_limit, conn))
            return conn

        result = probe_user_websocket(
            {"apiKey": "key", "secret": "secret-value", "passphrase": "pass"},
            ["condition-1"],
            timeout=4,
            connection_factory=factory,
        )

        self.assertEqual(connections[0][0], user_ws_url())
        self.assertEqual(connections[0][1], 4.0)
        self.assertEqual(connections[0][2], 0)
        self.assertEqual(connections[0][3].read_timeout, 4.0)
        self.assertTrue(result["connected"])
        self.assertTrue(result["subscription_sent"])
        self.assertTrue(connections[0][3].closed)
        self.assertEqual(call_order[0], "status")
        subscription = json.loads(connections[0][3].sent[0])
        self.assertEqual(subscription["markets"], ["condition-1"])
        self.assertEqual(subscription["auth"]["secret"], "secret-value")
        self.assertNotIn("secret-value", str(result))

    def test_user_websocket_probe_tolerates_a_missing_reply_and_closes_connection(self) -> None:
        class SilentConnection:
            def __init__(self) -> None:
                self.closed = False

            def getstatus(self) -> int:
                return 101

            def settimeout(self, _timeout: float) -> None:
                return None

            def send(self, message: str) -> None:
                return None

            def recv(self) -> str:
                raise TimeoutError("no reply")

            def close(self) -> None:
                self.closed = True

        connection = SilentConnection()
        result = probe_user_websocket(
            {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
            connection_factory=lambda *_args, **_kwargs: connection,
        )

        self.assertTrue(result["connected"])
        self.assertTrue(result["subscription_sent"])
        self.assertFalse(result["received_message"])
        self.assertEqual(result["message_sample_type"], "")
        self.assertTrue(connection.closed)

    def test_user_websocket_probe_rejects_redirect_before_forwarding_auth(self) -> None:
        class RedirectConnection:
            def __init__(self) -> None:
                self.sent = []
                self.closed = False

            def getstatus(self) -> int:
                return 302

            def send(self, message: str) -> None:
                self.sent.append(message)

            def close(self) -> None:
                self.closed = True

        connection = RedirectConnection()
        factory_calls = []

        def factory(url, *, timeout, redirect_limit):
            factory_calls.append((url, timeout, redirect_limit))
            return connection

        with self.assertRaisesRegex(
            ws_transport.WebSocketTransportError,
            "HTTP 302",
        ):
            probe_user_websocket(
                {
                    "apiKey": "key",
                    "secret": "must-not-be-forwarded",
                    "passphrase": "pass",
                },
                ["condition-1"],
                connection_factory=factory,
            )

        self.assertEqual(factory_calls[0][2], 0)
        self.assertEqual(connection.sent, [])
        self.assertTrue(connection.closed)

    def test_market_websocket_dispatches_and_syncs_canonical_subscriptions(self) -> None:
        events = []
        created = []
        connected_states = []

        class FakeConnection:
            def __init__(self) -> None:
                self.sent = []
                self.closed = False
                self.read_timeout = None
                self.recv_count = 0

            def getstatus(self) -> int:
                return 101

            def settimeout(self, timeout: float) -> None:
                self.read_timeout = timeout

            def send(self, message: str) -> None:
                self.sent.append(message)

            def recv(self) -> str:
                self.recv_count += 1
                if self.recv_count == 1:
                    connected_states.append(client.is_connected)
                    client.subscribe(["asset-2"])
                    client.unsubscribe(["asset-1"])
                    return "PING"
                if self.recv_count == 2:
                    return '{"event":"book"}'
                if self.recv_count == 3:
                    return "not-json"
                return ""

            def close(self) -> None:
                self.closed = True

        def factory(url, *, timeout, redirect_limit):
            connection = FakeConnection()
            created.append((url, timeout, redirect_limit, connection))
            return connection

        client = ws_market.MarketWSClient(
            ["asset-1"],
            events.append,
            custom_feature_enabled=True,
            url_base="wss://example.test/base/",
        )
        with patch.object(ws_market, "create_connection", factory):
            client._connect_once()

        connection = created[0][3]
        self.assertEqual(created[0][0], "wss://example.test/base/ws/market")
        self.assertEqual(created[0][1], ws_transport.WEBSOCKET_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(created[0][2], 0)
        self.assertEqual(connection.read_timeout, ws_transport.WEBSOCKET_IO_TIMEOUT_SECONDS)
        self.assertEqual(
            json.loads(connection.sent[0]),
            {
                "assets_ids": ["asset-1"],
                "type": "market",
                "custom_feature_enabled": True,
            },
        )
        self.assertEqual(connection.sent[1], "PING")
        self.assertEqual(
            json.loads(connection.sent[2]),
            {
                "assets_ids": ["asset-1"],
                "operation": "unsubscribe",
                "custom_feature_enabled": True,
            },
        )
        self.assertEqual(
            json.loads(connection.sent[3]),
            {
                "assets_ids": ["asset-2"],
                "operation": "subscribe",
                "custom_feature_enabled": True,
            },
        )
        self.assertEqual(events, [{"event": "book"}])
        self.assertEqual(connected_states, [True])
        self.assertFalse(client.is_connected)
        self.assertTrue(connection.closed)
        self.assertEqual(client._token_ids, {"asset-2"})

    def test_user_and_sports_websocket_clients_dispatch_events_and_protocol_pings(self) -> None:
        user_events = []
        sports_events = []
        user_created = []
        sports_created = []
        user_connected_states = []
        sports_connected_states = []

        class FakeConnection:
            def __init__(self, messages, connected_states, client_getter) -> None:
                self.messages = iter(messages)
                self.connected_states = connected_states
                self.client_getter = client_getter
                self.sent = []
                self.closed = False
                self.read_timeout = None
                self.recv_count = 0

            def getstatus(self) -> int:
                return 101

            def settimeout(self, timeout: float) -> None:
                self.read_timeout = timeout

            def send(self, message: str) -> None:
                self.sent.append(message)

            def recv(self) -> str:
                if self.recv_count == 0:
                    self.connected_states.append(self.client_getter().is_connected)
                self.recv_count += 1
                return next(self.messages)

            def close(self) -> None:
                self.closed = True

        def user_factory(url, *, timeout, redirect_limit):
            connection = FakeConnection(
                ["PONG", '{"event":"trade"}', "not-json", ""],
                user_connected_states,
                lambda: user,
            )
            user_created.append((url, timeout, redirect_limit, connection))
            return connection

        def sports_factory(url, *, timeout, redirect_limit):
            connection = FakeConnection(
                ["ping", '{"event":"score"}', "not-json", ""],
                sports_connected_states,
                lambda: sports,
            )
            sports_created.append((url, timeout, redirect_limit, connection))
            return connection

        user = ws_user.UserWSClient(
            {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
            ["condition-1"],
            user_events.append,
            url_base="wss://example.test/base/",
        )
        sports = ws_sports.SportsWSClient(sports_events.append, url_base="wss://sports.example.test/base/")
        with patch.object(ws_user, "create_connection", user_factory), patch.object(
            ws_sports,
            "create_connection",
            sports_factory,
        ):
            user._connect_once()
            sports._connect_once()

        user_connection = user_created[0][3]
        sports_connection = sports_created[0][3]
        self.assertEqual(user_created[0][0], "wss://example.test/base/ws/user")
        self.assertEqual(user_created[0][2], 0)
        self.assertEqual(json.loads(user_connection.sent[0])["markets"], ["condition-1"])
        self.assertEqual(user_connection.sent[1], "PING")
        self.assertEqual(user_events, [{"event": "trade"}])
        self.assertEqual(user_connected_states, [True])
        self.assertFalse(user.is_connected)
        self.assertTrue(user_connection.closed)
        self.assertEqual(sports_created[0][0], "wss://sports.example.test/base/ws")
        self.assertEqual(sports_created[0][2], 0)
        self.assertEqual(sports_connection.sent, ["pong"])
        self.assertEqual(sports_events, [{"event": "score"}])
        self.assertEqual(sports_connected_states, [True])
        self.assertFalse(sports.is_connected)
        self.assertTrue(sports_connection.closed)

    def test_market_websocket_replays_canonical_state_after_disconnect(self) -> None:
        client = ws_market.MarketWSClient(
            ["asset-1"],
            lambda _event: None,
            url_base="wss://example.test",
        )

        class FakeConnection:
            def __init__(self, *, mutate_state: bool) -> None:
                self.mutate_state = mutate_state
                self.sent = []
                self.closed = False

            def getstatus(self) -> int:
                return 101

            def settimeout(self, _timeout: float) -> None:
                return None

            def send(self, message: str) -> None:
                self.sent.append(message)

            def recv(self) -> str:
                if self.mutate_state:
                    self.mutate_state = False
                    client.set_tokens(["asset-2"])
                return ""

            def close(self) -> None:
                self.closed = True

        connections = [
            FakeConnection(mutate_state=True),
            FakeConnection(mutate_state=False),
        ]

        def factory(_url, *, timeout, redirect_limit):
            self.assertGreater(timeout, 0)
            self.assertEqual(redirect_limit, 0)
            return connections.pop(0)

        first_connection, second_connection = connections
        with patch.object(ws_market, "create_connection", factory):
            client._connect_once()
            client._connect_once()

        self.assertEqual(json.loads(first_connection.sent[0])["assets_ids"], ["asset-1"])
        self.assertEqual(json.loads(second_connection.sent[0])["assets_ids"], ["asset-2"])
        self.assertTrue(first_connection.closed)
        self.assertTrue(second_connection.closed)

    def test_websocket_reconnect_backoff_grows_for_short_connections_and_resets(self) -> None:
        client = ws_market.MarketWSClient(["asset-1"], lambda _event: None)
        connection_durations = iter(
            [
                0.0,
                0.0,
                ws_transport.WEBSOCKET_STABLE_CONNECTION_SECONDS,
                0.0,
            ]
        )

        class RecordingStopEvent:
            def __init__(self) -> None:
                self.stopped = False
                self.waits = []

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, delay: float) -> bool:
                self.waits.append(delay)
                if len(self.waits) == 4:
                    self.stopped = True
                return self.stopped

        stop_event = RecordingStopEvent()
        client._connect_once = lambda *_args: next(connection_durations)
        client._run(0, stop_event)

        self.assertEqual(
            stop_event.waits,
            [
                ws_transport.WEBSOCKET_INITIAL_BACKOFF_SECONDS,
                ws_transport.WEBSOCKET_INITIAL_BACKOFF_SECONDS * 2,
                ws_transport.WEBSOCKET_INITIAL_BACKOFF_SECONDS,
                ws_transport.WEBSOCKET_INITIAL_BACKOFF_SECONDS * 2,
            ],
        )

    def test_websocket_clients_stop_join_and_restart_cleanly(self) -> None:
        clients = (
            ws_market.MarketWSClient(["asset-1"], lambda _event: None),
            ws_sports.SportsWSClient(lambda _event: None),
            ws_user.UserWSClient(
                {"apiKey": "key", "secret": "secret", "passphrase": "pass"},
                ["condition-1"],
                lambda _event: None,
            ),
        )

        for client in clients:
            with self.subTest(client=type(client).__name__):
                entered = threading.Event()

                def block_until_stopped(
                    _generation,
                    stop_event,
                    current_entered=entered,
                ) -> float:
                    current_entered.set()
                    stop_event.wait()
                    return 0.0

                client._connect_once = block_until_stopped
                client.start()
                self.assertTrue(entered.wait(timeout=2))
                first_thread = client._thread
                self.assertTrue(client.is_running)
                client.stop()
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(client.is_running)
                self.assertFalse(client.is_connected)

                entered.clear()
                client.start()
                self.assertTrue(entered.wait(timeout=2))
                self.assertIsNot(client._thread, first_thread)
                self.assertTrue(client.is_running)
                client.stop()
                self.assertFalse(client._thread.is_alive())
                self.assertFalse(client.is_connected)

    def test_websocket_url_builders_reject_private_and_ambiguous_bases(self) -> None:
        for builder in (user_ws_url, sports_ws_url):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(PolymarketValidationError):
                    builder("wss://127.0.0.1")
                with self.assertRaises(PolymarketValidationError):
                    builder("wss://example.test/base?token=secret")

        with self.assertRaises(PolymarketValidationError):
            ws_market.MarketWSClient([], lambda _event: None, url_base="ws://127.0.0.1")

    def test_polymarket_official_api_coverage_manifest_uses_truthful_tiered_status(self) -> None:
        coverage = polymarket_official_api_coverage()
        self.assertEqual(coverage["docs_checked"], "2026-05-28")
        self.assertTrue(coverage["categories"])
        self.assertIn("polymarket.http_client", coverage["contract_hardening"]["modules"])
        self.assertIn("documented batch caps", " ".join(coverage["contract_hardening"]["features"]))
        self.assertEqual(coverage["authenticated_clob_readiness"]["api_route"], "/api/polymarket/clob-readiness")
        self.assertIn("polymarket.auth_readiness", coverage["authenticated_clob_readiness"]["module"])
        self.assertEqual(coverage["live_order_cancel_harness"]["default_mode"], "dry_run_transcript")
        self.assertEqual(coverage["live_order_cancel_harness"]["hard_caps"]["max_notional_usdc"], 1.0)
        self.assertEqual(coverage["live_credential_validation"]["default_mode"], "no_funded_actions")
        self.assertIn("polymarket.credential_runbook", coverage["live_credential_validation"]["module"])
        self.assertIn("polymarket.live_reports", coverage["live_credential_validation"]["module"])
        self.assertIn("runbook_command", coverage["live_credential_validation"])
        self.assertIn("promotion_guard", coverage["live_credential_validation"])
        self.assertIn("credential_runbook", coverage["live_credential_validation"]["report_fields"])
        self.assertIn("verification_promotion", coverage["live_credential_validation"]["report_fields"])
        self.assertIn("stage_gates", coverage["live_credential_validation"]["report_fields"])
        self.assertEqual(coverage["historical_mdd_v2"]["method"], "public_data_historical_equity_curve_v2")
        self.assertIn("/api/polymarket/users/mdd", coverage["historical_mdd_v2"]["api_routes"])
        self.assertEqual(coverage["historical_mark_replay_mdd"]["method"], "clob_price_history_inventory_mark_replay_v1")
        self.assertEqual(coverage["historical_mark_replay_mdd"]["default"], "off")
        self.assertEqual(coverage["accounting_snapshot_reconciliation"]["module"], "polymarket.accounting")
        self.assertEqual(coverage["accounting_snapshot_reconciliation"]["default"], "off")
        self.assertEqual(coverage["analytics_cache_exports"]["module"], "polymarket.analytics_cache")
        self.assertIn("/api/polymarket/users/mdd/export.csv", coverage["analytics_cache_exports"]["api_routes"])
        expected_levels = set(coverage["coverage_level_definitions"])
        allowed_states = set(coverage["coverage_state_definitions"])
        self.assertEqual(
            expected_levels,
            {
                "wrapper_available",
                "app_workflow_available",
                "offline_tested",
                "public_live_verified",
                "credential_live_verified",
                "funded_live_verified",
            },
        )
        for item in coverage["categories"]:
            self.assertIn("truthful_status", item)
            self.assertEqual(set(item["coverage_levels"]), expected_levels)
            self.assertTrue(set(item["coverage_levels"].values()).issubset(allowed_states))
        self.assertIn("blocked", {item["coverage_levels"]["credential_live_verified"] for item in coverage["categories"]})
        self.assertNotIn("yes", {item["coverage_levels"]["funded_live_verified"] for item in coverage["categories"]})
        modules = " ".join(item["module"] for item in coverage["categories"])
        self.assertIn("polymarket.bridge", modules)
        self.assertIn("polymarket.relayer", modules)


if __name__ == "__main__":
    unittest.main()
