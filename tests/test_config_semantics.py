from __future__ import annotations

from copy import deepcopy
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import unittest

from core.models import AppConfig, CopyActivityOutboxEntry, CopyTradeSettings, MutationJournalEntry
from core.storage import ConfigLoadError, load_config, save_config


class ConfigSemanticsTests(unittest.TestCase):
    def assert_invalid_file(self, payload: dict) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = json.dumps(payload).encode("utf-8")
            path.write_bytes(raw)
            with self.assertRaises(ConfigLoadError):
                load_config(path)
            self.assertEqual(path.read_bytes(), raw)

    @staticmethod
    def journal() -> dict:
        return MutationJournalEntry(
            key_hash="a" * 64, request_hash="b" * 64,
            method="POST", path="/api/markets/kalshi/orders/cancel_order", live=True,
        ).to_dict()

    @staticmethod
    def outbox() -> dict:
        return CopyActivityOutboxEntry(
            watch_id="watch-1", activity_key="tx:1", activity={"transactionHash": "1"},
        ).to_dict()

    def test_copy_flags_require_real_booleans(self) -> None:
        for field in ("enabled", "live", "allow_sells", "conflict_guard"):
            for value in ("false", "true", "", 0, 1, None, [], {}):
                with self.subTest(field=field, value=value):
                    self.assert_invalid_file({"copytrading": {field: value}})
            for value in (True, False):
                self.assertIs(getattr(CopyTradeSettings.from_dict({field: value}), field), value)

    def test_invalid_copy_risk_values_never_default_or_clamp(self) -> None:
        bounds = {
            "copy_percentage": [-1, 101], "scale": [-0.1, 2],
            "max_usdc_per_trade": [0, -5], "slippage": [-0.1, 1.1],
            "conflict_window_seconds": [-1, 86401, 0.5, "0.5"],
        }
        for field, invalid in bounds.items():
            for value in [*invalid, "invalid", "", None, True, [], {}, "nan", "inf", "1e999"]:
                with self.subTest(field=field, value=value):
                    self.assert_invalid_file({"copytrading": {field: value}})

    def test_legacy_numeric_strings_and_boundaries_remain_supported(self) -> None:
        for scale in (0.0, 1.0, 0.123456789012345):
            settings = CopyTradeSettings.from_dict({
                "scale": str(scale), "max_usdc_per_trade": "25.5",
                "slippage": "0", "conflict_window_seconds": "86400",
                "follow_wallets": "manifold:alice;solana:CaseSensitiveAddress",
            })
            restored = CopyTradeSettings.from_dict(settings.to_dict())
            self.assertAlmostEqual(restored.scale, scale, places=12)
            self.assertEqual(restored.max_usdc_per_trade, 25.5)
            self.assertEqual(restored.conflict_window_seconds, 86400)
            self.assertEqual(restored.follow_wallets, ["manifold:alice", "solana:CaseSensitiveAddress"])
        self.assertEqual(CopyTradeSettings.from_dict({"copy_percentage": "25"}).scale, 0.25)

    def test_conflicting_percentage_and_scale_are_rejected(self) -> None:
        self.assert_invalid_file({"copytrading": {"scale": 0.1, "copy_percentage": 100}})
        self.assert_invalid_file({"copytrading": {"scale": "invalid", "copy_percentage": 25}})

    def test_invalid_in_memory_copy_settings_cannot_be_laundered_by_serialization(self) -> None:
        values = {"scale": (2, -1, float("nan"), float("inf")), "live": ("false",),
                  "slippage": (-1,), "max_usdc_per_trade": (0,), "conflict_window_seconds": (0.5,)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(AppConfig(), path)
            raw = path.read_bytes()
            for field, invalid in values.items():
                for value in invalid:
                    with self.subTest(field=field, value=value):
                        cfg = load_config(path)
                        setattr(cfg.copytrading, field, value)
                        with self.assertRaises(ValueError):
                            save_config(cfg, path)
                        self.assertEqual(path.read_bytes(), raw)

    def test_malformed_follow_lists_cannot_be_silently_discarded(self) -> None:
        for value in (None, 1, {}, [None], [False], [{"wallet": "unexpected"}]):
            with self.subTest(value=value):
                self.assert_invalid_file({"copytrading": {"follow_wallets": value}})

    def test_other_operational_flags_require_booleans(self) -> None:
        for payload in (
            {"markets": {"kalshi": {"enabled": "false"}}},
            {"wallets": [{"wallet": "0x" + "a" * 40, "enabled": "false"}]},
            {"alerts": [{"token_id": "t", "label": "a", "direction": "above", "threshold": 0.5, "enabled": "false"}]},
            {"paper_trades": [{"accepted": "false"}]},
        ):
            with self.subTest(payload=payload):
                self.assert_invalid_file(payload)

    def test_journal_requires_identity_and_explicit_execution_classification(self) -> None:
        original = self.journal()
        self.assert_invalid_file({"mutation_journal": [{}]})
        for field in ("id", "key_hash", "request_hash", "method", "path", "live"):
            for value in (None, "", [], {}):
                with self.subTest(field=field, value=value):
                    record = {**original, field: value}
                    self.assert_invalid_file({"mutation_journal": [record]})
            record = dict(original)
            del record[field]
            self.assert_invalid_file({"mutation_journal": [record]})
        for field, value in (("key_hash", "short"), ("request_hash", "g" * 64),
                             ("method", "GET"), ("path", "https://example.com/api/orders"), ("live", "false")):
            with self.subTest(field=field):
                self.assert_invalid_file({"mutation_journal": [{**original, field: value}]})

    def test_journal_metadata_cannot_synthesize_replay_authorization_or_success(self) -> None:
        original = self.journal()
        for field in ("created_at", "updated_at", "replay_authorized_at", "response_status"):
            for value in (-1, 0.5, True, "invalid", None):
                with self.subTest(field=field, value=value):
                    self.assert_invalid_file({"mutation_journal": [{**original, field: value}]})
        self.assert_invalid_file({"mutation_journal": [{**original, "state": "completed", "response_status": 0}]})
        self.assert_invalid_file({"mutation_journal": [{**original, "response": []}]})

    def test_unknown_journal_state_remains_nonreplayable(self) -> None:
        for state in (None, "future-state", ""):
            record = {**self.journal(), "state": state}
            loaded = AppConfig.from_dict({"mutation_journal": [record]})
            self.assertEqual(loaded.mutation_journal[0].state, "ambiguous")

    def test_duplicate_journal_ids_or_client_keys_are_rejected(self) -> None:
        first = self.journal()
        for second in ({**first, "key_hash": "c" * 64}, {**first, "id": "different-id"}):
            with self.subTest(second=second):
                self.assert_invalid_file({"mutation_journal": [first, second]})

    def test_outbox_requires_stable_identity_and_preserves_uncertain_state(self) -> None:
        original = self.outbox()
        self.assert_invalid_file({"copy_activity_outbox": [{}]})
        for field in ("id", "market_id", "watch_id", "activity_key", "activity"):
            record = dict(original)
            del record[field]
            self.assert_invalid_file({"copy_activity_outbox": [record]})
        for field in ("activity", "dispatch", "execution_policy"):
            self.assert_invalid_file({"copy_activity_outbox": [{**original, field: []}]})
        for state in (None, "", "future-state"):
            record = {**original, "state": state}
            loaded = AppConfig.from_dict({"copy_activity_outbox": [record]})
            self.assertEqual(loaded.copy_activity_outbox[0].state, "ambiguous")
        record = dict(original)
        del record["state"]
        self.assertEqual(AppConfig.from_dict({"copy_activity_outbox": [record]}).copy_activity_outbox[0].state, "ambiguous")

    def test_outbox_policy_and_attempt_metadata_are_validated(self) -> None:
        original = self.outbox()
        self.assert_invalid_file({"copy_activity_outbox": [{**original, "execution_policy": {"live": "false"}}]})
        self.assert_invalid_file({"copy_activity_outbox": [{**original, "attempts": -1}]})
        self.assert_invalid_file({"copy_activity_outbox": [{**original, "replay_authorized_at": True}]})

    def test_duplicate_outbox_ids_or_signal_keys_are_rejected(self) -> None:
        first = self.outbox()
        for second in ({**first, "activity_key": "tx:2"}, {**first, "id": "different-id"}):
            self.assert_invalid_file({"copy_activity_outbox": [first, second]})

    def test_save_rejects_duplicate_identity_without_replacing_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(AppConfig(), path)
            original = path.read_bytes()
            cfg = load_config(path)
            entry = MutationJournalEntry.from_dict(self.journal())
            cfg.mutation_journal = [entry, deepcopy(entry)]
            with self.assertRaises(ValueError):
                save_config(cfg, path)
            self.assertEqual(path.read_bytes(), original)

    def test_internal_http_status_enum_roundtrips_without_accepting_boolean_status(self) -> None:
        entry = MutationJournalEntry.from_dict(self.journal())
        entry.state = "completed"
        entry.response_status = HTTPStatus.OK
        entry.response = {"ok": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(AppConfig(mutation_journal=[entry]), path)
            cfg = load_config(path)
            self.assertEqual(cfg.mutation_journal[0].response_status, 200)
            self.assertIs(type(cfg.mutation_journal[0].response_status), int)
            original = path.read_bytes()
            cfg.mutation_journal[0].response_status = True
            with self.assertRaises(ValueError):
                save_config(cfg, path)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_in_memory_response_is_not_wrapped_as_successful_result(self) -> None:
        cfg = AppConfig(mutation_journal=[MutationJournalEntry.from_dict(self.journal())])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(cfg, path)
            original = path.read_bytes()
            cfg.mutation_journal[0].response = []
            with self.assertRaises(ValueError):
                save_config(cfg, path)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
