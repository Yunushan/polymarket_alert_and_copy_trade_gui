from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from polymarket import live_verification
from scripts import verify_polymarket_live as live


class FundedRecoveryJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.target = self.parent / "recovery.json"
        if os.name == "posix":
            self.parent.chmod(0o700)

    @staticmethod
    def resolved_record() -> dict:
        return {
            "schema_version": 1, "market_id": "polymarket", "stage": "cancel_verified",
            "resolved": True, "manual_reconciliation_required": False, "zero_fill_verified": True,
            "post_only": True, "tif": "GTC", "side": "BUY", "price": 0.01, "size": 1.0,
            "token_id": "123", "order_id": "0x" + "2" * 64, "account_address": "0x" + "1" * 40,
            "source_revision": "b" * 40, "run_id": "8fa2660a-4a06-4b3a-9f25-6534b07351b5",
            "sequence": 3, "run_started_at": "2026-09-06T10:00:00Z", "updated_at": "2026-09-06T10:00:01Z",
        }

    def store(self, raw: str) -> None:
        self.target.write_text(raw, encoding="utf-8")
        if os.name == "posix":
            self.target.chmod(0o600)

    @contextmanager
    def session(self, source_revision: str = "a" * 40):
        # Exercise the file contract on Windows without claiming its ACLs are accepted.
        privacy = patch.object(live, "_enforce_windows_recovery_journal_privacy") if os.name == "nt" else nullcontext()
        with privacy, live._recovery_journal_session(self.target, source_revision=source_revision) as write:
            yield write

    def assert_rejected_without_change(self, raw: str) -> None:
        self.store(raw)
        with self.assertRaises(ValueError):
            with self.session():
                self.fail("An invalid journal must not authorize another audit.")
        self.assertEqual(self.target.read_text(encoding="utf-8"), raw)
        self.assertEqual(list(self.parent.glob("*.resolved-*")), [])
        self.assertFalse((self.parent / ".recovery.json.funded.lock").exists())

    def test_duplicate_resolution_cannot_authorize_another_audit(self) -> None:
        self.assert_rejected_without_change('{"resolved": false, "resolved": true}')

    def test_missing_and_contradictory_resolution_evidence_is_preserved(self) -> None:
        for key in self.resolved_record():
            with self.subTest(missing=key):
                record = self.resolved_record()
                del record[key]
                self.assert_rejected_without_change(json.dumps(record))
        changes = (
            ("resolved", 1), ("resolved", False), ("schema_version", True), ("schema_version", 2),
            ("stage", "cancel_incomplete"), ("zero_fill_verified", False), ("manual_reconciliation_required", True),
            ("post_only", False), ("tif", "FOK"), ("market_id", "another-market"), ("side", "unknown"),
            ("price", 0), ("price", 1), ("price", True), ("price", "0.01"), ("size", -1),
            ("size", "1"), ("size", 10 ** 500), ("sequence", True), ("sequence", 0),
            ("token_id", ""), ("order_id", "\n"), ("order_id", "order-1"), ("order_id", "x" * 257),
            ("account_address", "not-an-account"),
            ("source_revision", "main"), ("run_id", "unknown"), ("updated_at", "2026-09-06T09:59:59Z"),
            ("run_started_at", "2026-09-06T10:00:00"),
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.assert_rejected_without_change(json.dumps({**self.resolved_record(), key: value}))

    def test_nonfinite_nested_duplicate_malformed_and_oversized_json_are_rejected(self) -> None:
        valid = json.dumps(self.resolved_record())
        for raw in (
            '{', '[]', 'true', '{"resolved": true}',
            valid[:-1] + ', "extra": NaN}', valid[:-1] + ', "extra": Infinity}',
            valid[:-1] + ', "extra": 1e10000}', valid[:-1] + ', "extra": {"x": 1, "x": 2}}',
            ' ' * (live.MAX_RECOVERY_JOURNAL_BYTES + 1),
        ):
            with self.subTest(prefix=raw[:50]):
                self.assert_rejected_without_change(raw)

    def test_valid_prior_revision_is_archived_and_new_identity_is_bound(self) -> None:
        original = json.dumps(self.resolved_record())
        self.store(original)
        with self.session() as write:
            self.assertFalse(self.target.exists())
            archives = list(self.parent.glob("*.resolved-*"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_text(encoding="utf-8"), original)
            pending = {**self.resolved_record(), "stage": "placement_pending", "order_id": "",
                       "resolved": False, "manual_reconciliation_required": True}
            write(pending)
            saved = json.loads(self.target.read_text(encoding="utf-8"))
            self.assertEqual(saved["source_revision"], "a" * 40)
            self.assertNotEqual(saved["run_id"], pending["run_id"])
            self.assertEqual(saved["sequence"], 1)
            write(self.resolved_record())
            live._read_resolved_recovery_journal(self.target)
            self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))["sequence"], 2)
            if os.name == "posix":
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)

    def test_invalid_resolution_or_serialization_preserves_pending_state(self) -> None:
        with self.session() as write:
            write({**self.resolved_record(), "stage": "placement_pending", "resolved": False,
                   "manual_reconciliation_required": True, "order_id": ""})
            original = self.target.read_bytes()
            for payload in (
                {"resolved": True}, {**self.resolved_record(), "zero_fill_verified": False},
                {"resolved": False, "extra": float("nan")},
                {"resolved": False, "extra": "x" * live.MAX_RECOVERY_JOURNAL_BYTES},
            ):
                with self.subTest(payload_keys=list(payload)), self.assertRaises(ValueError):
                    write(payload)
                self.assertEqual(self.target.read_bytes(), original)
            with patch.object(live, "atomic_write_text", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    write(self.resolved_record())
            self.assertEqual(self.target.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "unresolved"):
            with self.session():
                self.fail("Pending state must still require reconciliation.")

    def test_missing_source_identity_fails_before_touching_prior_journal(self) -> None:
        original = json.dumps(self.resolved_record())
        self.store(original)
        for source in ("", "main", "a" * 39):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "exact source"):
                with self.session(source):
                    self.fail("Missing source identity must not open a journal.")
            self.assertEqual(self.target.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.parent.glob("*.resolved-*")), [])

    def test_simultaneous_session_is_rejected_and_lock_is_released(self) -> None:
        with self.session():
            with self.assertRaisesRegex(ValueError, "locked"):
                with self.session():
                    self.fail("A second session must not be admitted.")
        self.assertFalse((self.parent / ".recovery.json.funded.lock").exists())

    @patch.object(live_verification, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True)
    def test_offline_order_cancel_harness_produces_a_reopenable_journal(self) -> None:
        order_id = "0x" + "2" * 64
        trader = Mock(spec=["get_trading_account_address", "get_orders", "get_trading_balance_allowance",
                            "place_limit_order", "cancel_order", "get_order"])
        trader.get_trading_account_address.return_value = "0x" + "1" * 40
        trader.get_orders.return_value = []
        trader.get_trading_balance_allowance.return_value = {"balance": "10000", "allowances": {"exchange": "10000"}}
        trader.cancel_order.return_value = {"canceled": [order_id]}
        trader.get_order.return_value = {"id": order_id, "status": "ORDER_STATUS_CANCELED",
                                        "size_matched": "0", "associate_trades": []}

        def place(**kwargs):
            pending = json.loads(self.target.read_text(encoding="utf-8"))
            self.assertEqual(pending["stage"], "placement_pending")
            self.assertIs(pending["resolved"], False)
            self.assertIs(kwargs["post_only"], True)
            return {"orderID": order_id}

        trader.place_limit_order.side_effect = place
        request = live_verification.LiveOrderCancelRequest(
            token_id="123", side="BUY", price="0.01", size="1", allow_token_ids=["123"],
            private_key="0x" + "1" * 64, execute=True, cancel_immediately=True,
            confirmation=live_verification.CONFIRM_LIVE_ORDER_CANCEL,
        )
        with self.session() as write:
            result = live_verification.run_live_order_cancel_verification(
                request, trader_factory=lambda _config: trader,
                orderbook_getter=lambda _token: {"bids": [{"price": "0.02"}], "asks": [{"price": "0.04"}]},
                geoblock_checker=lambda: {"blocked": False}, recovery_writer=write,
            )
        self.assertEqual(result["status"], "ok", result)
        trader.cancel_order.assert_called_once_with(order_id)
        live._read_resolved_recovery_journal(self.target)
        original = self.target.read_bytes()
        with self.session("c" * 40):
            archives = list(self.parent.glob("*.resolved-*"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows-specific ACL policy")
    def test_real_windows_policy_remains_disabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner-only directory ACL"):
            with live._recovery_journal_session(self.target, source_revision="a" * 40):
                self.fail("Windows funded journals must remain disabled.")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_posix_parent_and_file_must_be_private(self) -> None:
        self.parent.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "group/world"):
            with self.session():
                self.fail("A public directory must be rejected.")
        self.parent.chmod(0o700)
        self.store(json.dumps(self.resolved_record()))
        self.target.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "group/world"):
            with self.session():
                self.fail("A public journal must be rejected.")


if __name__ == "__main__":
    unittest.main()
