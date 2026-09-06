from __future__ import annotations

import tempfile
import json
from stat import S_IFDIR, S_IFIFO, S_IFLNK
from types import SimpleNamespace
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config_security import ConfigSecurityError, is_sensitive_config_key, is_sensitive_display_key
from core.models import (
    AppConfig,
    CopyActivityOutboxEntry,
    CopyTradeSettings,
    MarketConfig,
    MAX_MUTATION_JOURNAL_ENTRIES,
    MutationJournalEntry,
    PaperTradeRecord,
    PriceAlert,
    WalletWatch,
)
from core.storage import (
    CONFIG_PATH_ENV,
    ConfigConflictError,
    ConfigLoadError,
    _fsync_parent_directory,
    default_config_path,
    load_config,
    save_config,
)
from market_adapters import MARKET_IDS
from polymarket.util import is_wallet_address, normalize_wallet


WALLET = "0x" + "a" * 40


class CoreModelTests(unittest.TestCase):
    def test_wallet_validation_and_normalization(self) -> None:
        self.assertTrue(is_wallet_address(WALLET))
        self.assertEqual(normalize_wallet(WALLET.upper().replace("X", "x", 1)), WALLET)
        self.assertFalse(is_wallet_address("0x123"))
        self.assertIsNone(normalize_wallet("not-a-wallet"))

    def test_config_roundtrip_preserves_alert_wallet_and_copy_settings(self) -> None:
        cfg = AppConfig(
            alerts=[
                PriceAlert(
                    token_id="token-1",
                    label="Yes alert",
                    direction="above",
                    threshold=0.55,
                    source="last_trade",
                )
            ],
            paper_trades=[
                PaperTradeRecord(
                    market_id="kalshi",
                    contract_id="FED-YES:yes",
                    side="BUY",
                    size=3.0,
                    limit_price=0.44,
                    accepted=True,
                    message="DRY RUN",
                    raw={"request": {"ticker": "FED-YES"}},
                )
            ],
            wallets=[
                WalletWatch(
                    wallet=WALLET,
                    display_name="tracked",
                    last_seen_ts=123,
                    last_seen_tx="tx1",
                    seen_activity_keys=["tx:tx1"],
                )
            ],
            copytrading=CopyTradeSettings(
                enabled=True,
                live=False,
                follow_wallet=WALLET,
                follow_wallets=[WALLET],
                scale=0.5,
                max_usdc_per_trade=10.0,
            ),
            selected_market_id="kalshi",
            theme="dark",
            ui_design="sentinel_2027",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertEqual(loaded.theme, "dark")
        self.assertEqual(loaded.ui_design, "sentinel_2027")
        self.assertEqual(loaded.selected_market_id, "kalshi")
        self.assertEqual(len(loaded.alerts), 1)
        self.assertEqual(loaded.alerts[0].threshold, 0.55)
        self.assertEqual(loaded.alerts[0].market_id, "polymarket")
        self.assertEqual(len(loaded.paper_trades), 1)
        self.assertEqual(loaded.paper_trades[0].market_id, "kalshi")
        self.assertEqual(loaded.paper_trades[0].limit_price, 0.44)
        self.assertEqual(loaded.paper_trades[0].raw["request"]["ticker"], "FED-YES")
        self.assertEqual(len(loaded.wallets), 1)
        self.assertEqual(loaded.wallets[0].seen_activity_keys, ["tx:tx1"])
        self.assertIn("polymarket", loaded.markets)
        self.assertTrue(loaded.markets["polymarket"].enabled)
        self.assertTrue(loaded.copytrading.enabled)
        self.assertFalse(loaded.copytrading.live)
        self.assertEqual(loaded.copytrading.normalized_follow_wallets(), [WALLET])
        self.assertEqual(loaded.copytrading.to_dict()["copy_percentage"], 50.0)

    def test_copy_settings_load_percentage_and_valid_legacy_scale(self) -> None:
        from_percentage = CopyTradeSettings.from_dict({"copy_percentage": 25})
        from_legacy = CopyTradeSettings.from_dict({"scale": 0.5})

        self.assertEqual(from_percentage.scale, 0.25)
        self.assertEqual(from_legacy.scale, 0.5)
        with self.assertRaises(ValueError):
            CopyTradeSettings.from_dict({"scale": 2.0})

    def test_copy_activity_outbox_roundtrip_preserves_manual_reconciliation_state(self) -> None:
        entry = CopyActivityOutboxEntry(
            watch_id="watch-1",
            activity_key="tx:abc",
            activity={"transactionHash": "abc", "side": "BUY", "asset": "token-1"},
            state="ambiguous",
            attempts=2,
            outcome_code="live_dispatch_ambiguous",
            outcome_message="Manual reconciliation required.",
            dispatch={
                "market_id": "polymarket",
                "contract_id": "token-1",
                "side": "BUY",
                "size": 1.25,
                "limit_price": 0.5,
                "tif": "FOK",
            },
        )
        cfg = AppConfig(copy_activity_outbox=[entry])

        loaded = AppConfig.from_dict(cfg.to_dict())

        self.assertEqual(len(loaded.copy_activity_outbox), 1)
        restored = loaded.copy_activity_outbox[0]
        self.assertEqual(restored.state, "ambiguous")
        self.assertEqual(restored.attempts, 2)
        self.assertEqual(restored.outcome_code, "live_dispatch_ambiguous")
        self.assertEqual(restored.activity["transactionHash"], "abc")
        self.assertEqual(restored.dispatch["contract_id"], "token-1")

    def test_copy_activity_outbox_unknown_state_fails_closed(self) -> None:
        loaded = CopyActivityOutboxEntry.from_dict(
            {
                "id": "outbox-1",
                "market_id": "polymarket",
                "watch_id": "watch-1",
                "activity_key": "tx:abc",
                "activity": {"transactionHash": "abc"},
                "state": "future-dispatch-state",
            }
        )

        self.assertEqual(loaded.state, "ambiguous")

    def test_legacy_config_without_copy_activity_outbox_remains_loadable(self) -> None:
        loaded = AppConfig.from_dict({"wallets": [{"wallet": WALLET}]})

        self.assertEqual(loaded.copy_activity_outbox, [])
        self.assertEqual(loaded.wallets[0].wallet, WALLET)

    def test_copy_settings_preserve_multiple_follow_wallets(self) -> None:
        other = "0x" + "b" * 40
        settings = CopyTradeSettings.from_dict(
            {
                "follow_wallet": WALLET,
                "follow_wallets": [other, WALLET],
                "conflict_guard": True,
                "conflict_window_seconds": 120,
            }
        )

        self.assertEqual(settings.normalized_follow_wallets(), [WALLET, other])
        self.assertEqual(settings.to_dict()["follow_wallet"], WALLET)
        self.assertEqual(settings.to_dict()["follow_wallets"], [WALLET, other])

    def test_default_config_includes_all_catalog_markets(self) -> None:
        cfg = AppConfig()

        self.assertEqual(set(cfg.markets), set(MARKET_IDS))
        self.assertEqual(cfg.selected_market_id, "polymarket")
        self.assertTrue(cfg.markets["polymarket"].enabled)
        disabled = [mid for mid, market_cfg in cfg.markets.items() if not market_cfg.enabled]
        self.assertGreater(len(disabled), 0)

    def test_market_config_roundtrip_preserves_unknown_settings(self) -> None:
        cfg = MarketConfig(
            market_id="kalshi",
            enabled=True,
            settings={"api_key_env": "KALSHI_API_KEY"},
        )

        loaded = MarketConfig.from_dict("kalshi", cfg.to_dict())

        self.assertEqual(loaded.market_id, "kalshi")
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.settings, {"api_key_env": "KALSHI_API_KEY"})

    def test_secret_key_policy_distinguishes_credentials_from_market_identifiers(self) -> None:
        credential_keys = (
            "betfair_app_key",
            "betfair_session_token",
            "betmgm_access_id",
            "betmgm_access_id_token",
            "context_api_key",
            "crypto_com_predict_api_key",
            "dflow_api_key",
            "draftkings_predictions_api_key",
            "fanatics_markets_api_key",
            "fanduel_predicts_api_key",
            "gemini_api_key",
            "gemini_api_secret",
            "good_judgment_open_api_token",
            "good_judgment_open_password",
            "ibkr_access_token",
            "ibkr_session_cookie",
            "kalshi_api_key_id",
            "kalshi_private_key_password",
            "kalshi_private_key_pem",
            "limitless_token_id",
            "limitless_token_secret",
            "manifold_api_key",
            "matchbook_mfa_code",
            "matchbook_password",
            "matchbook_session_token",
            "metaculus_api_token",
            "myriad_access_token",
            "myriad_api_key",
            "myriad_api_secret",
            "nadex_api_key",
            "opinion_api_key",
            "opinion_private_key",
            "POLY_PASSPHRASE",
            "POLY_SIGNATURE",
            "polymarket_private_key",
            "predict_fun_api_key",
            "predict_fun_jwt",
            "private_key",
            "probable_api_key",
            "probable_api_passphrase",
            "probable_api_secret",
            "prophet_exchange_access_key",
            "prophet_exchange_access_token",
            "prophet_exchange_api_key",
            "prophet_exchange_secret_key",
            "scicast_api_key",
            "smarkets_session_token",
            "sx_bet_api_key",
            "sx_bet_private_key",
            "xmarket_api_key",
            "xo_api_key",
            "xo_api_secret",
        )
        for key in credential_keys:
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_config_key(key))
        for key in (
            "token_id",
            "sx_bet_base_token",
            "private_market",
            "api_key_env",
            "credential_env_vars",
            "polymarket_signature_type",
            "dflow_user_public_key",
            "funder_address",
            "matchbook_username",
        ):
            with self.subTest(key=key):
                self.assertFalse(is_sensitive_config_key(key))
        self.assertTrue(is_sensitive_display_key("kalshi_private_key_path"))
        self.assertTrue(is_sensitive_display_key("auth_headers"))

    def test_save_config_rejects_persisted_credentials_without_writing_them(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].settings["kalshi_api_key_id"] = "do-not-persist"

        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "new-state-directory"
            path = parent / "config.json"
            with self.assertRaisesRegex(ConfigSecurityError, "environment variables") as ctx:
                save_config(cfg, path)

            self.assertFalse(parent.exists())
            self.assertFalse(path.exists())
            self.assertNotIn("do-not-persist", str(ctx.exception))

    def test_load_config_rejects_nested_persisted_credentials_without_echoing_them(self) -> None:
        secret = "nested-do-not-echo"
        payload = {
            "markets": {
                "polymarket": {
                    "enabled": True,
                    "settings": {"nested": {"POLY_PASSPHRASE": secret}},
                }
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigLoadError, "cannot be loaded") as ctx:
                load_config(path)

            self.assertNotIn(secret, str(ctx.exception))

    def test_config_persistence_allows_environment_references(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].settings.update(
            {
                "api_key_env": "KALSHI_API_KEY_ID",
                "credential_env_vars": ["KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PEM"],
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertEqual(loaded.markets["kalshi"].settings["api_key_env"], "KALSHI_API_KEY_ID")

    def test_config_persistence_rejects_invalid_environment_references_and_embedded_key_material(self) -> None:
        invalid_values = (
            {"credential_env_vars": ["not-an-environment-name"]},
            {"private_key_path": "-----BEGIN PRIVATE KEY-----\nsecret"},
            {"callback_url": "https://user:password@example.com/path"},
            {"credential_sources": ["actual-secret-value"]},
            {"auth_headers": [["Authorization", "Bearer secret"]]},
            {"notes": ["abcdefgh.ijklmnop.qrstuvwx"]},
        )
        for settings in invalid_values:
            with self.subTest(settings=tuple(settings)):
                cfg = AppConfig()
                cfg.markets["kalshi"].settings.update(settings)
                with tempfile.TemporaryDirectory() as temp_dir:
                    with self.assertRaises(ConfigSecurityError):
                        save_config(cfg, Path(temp_dir) / "config.json")

    def test_corrupt_config_fails_closed_and_preserves_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(ConfigLoadError, "Configuration file cannot be loaded"):
                load_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_malformed_durable_collections_fail_closed_without_losing_bytes(self) -> None:
        for key in ("copy_activity_outbox", "mutation_journal"):
            for value in ({"lost-record": {}}, [None], ["lost-record"], None):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    raw = json.dumps({key: value}).encode("utf-8")
                    path.write_bytes(raw)
                    with self.assertRaises(ConfigLoadError):
                        load_config(path)
                    self.assertEqual(path.read_bytes(), raw)

    def test_oversized_journal_cannot_drop_old_pending_live_operation_on_load(self) -> None:
        pending = MutationJournalEntry(
            key_hash="a" * 64, method="POST", path="/api/live/order",
            request_hash="b" * 64, live=True, state="pending", updated_at=1,
        )
        completed = MutationJournalEntry(
            key_hash="c" * 64, method="POST", path="/api/config",
            request_hash="d" * 64, state="completed", updated_at=2,
        )
        raw = json.dumps({"mutation_journal": [pending.to_dict()] + [completed.to_dict()] * MAX_MUTATION_JOURNAL_ENTRIES})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ConfigLoadError):
                load_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_duplicate_json_keys_cannot_replace_live_journal_or_safety_settings(self) -> None:
        for raw in (
            '{"mutation_journal":[{"live":true,"state":"pending"}],"mutation_journal":[]}',
            '{"markets":{"polymarket":{"settings":{"live_trading_enabled":false,"live_trading_enabled":true}}}}',
        ):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ConfigLoadError):
                    load_config(path)
                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_nonfinite_config_numbers_fail_closed_on_load_and_save(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                raw = '{"markets":{"polymarket":{"settings":{"live_trading_max_size":' + token + '}}}}'
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ConfigLoadError):
                    load_config(path)
                self.assertEqual(path.read_text(encoding="utf-8"), raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(AppConfig(), path)
            original = path.read_bytes()
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(value=value):
                    cfg = load_config(path)
                    cfg.markets["polymarket"].settings["live_trading_max_size"] = value
                    with self.assertRaises(ValueError):
                        save_config(cfg, path)
                    self.assertEqual(path.read_bytes(), original)

    def test_invalid_configuration_containers_do_not_default_silently(self) -> None:
        invalid = [
            {"markets": []}, {"markets": None}, {"markets": {"polymarket": None}},
            {"markets": {"polymarket": {"settings": []}}},
            {"copytrading": None}, {"copytrading": []},
        ]
        invalid.extend({key: [None]} for key in ("alerts", "wallets", "paper_trades"))
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AppConfig.from_dict(payload)

    def test_full_journal_roundtrip_and_retention_preserve_pending_live_record(self) -> None:
        pending = MutationJournalEntry(
            key_hash="a" * 64, method="POST", path="/api/live/order", request_hash="b" * 64,
            live=True, state="pending", updated_at=1,
        )
        cfg = AppConfig(mutation_journal=[pending])
        for index in range(MAX_MUTATION_JOURNAL_ENTRIES - 1):
            cfg.mutation_journal.append(MutationJournalEntry(
                key_hash=f"{index:064x}", method="POST", path="/api/config", request_hash="d" * 64,
                state="completed", response_status=200, updated_at=2,
            ))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(len(loaded.mutation_journal), MAX_MUTATION_JOURNAL_ENTRIES)
            self.assertEqual(loaded.mutation_journal[0].id, pending.id)
            self.assertTrue(loaded.mutation_journal[0].live)
            self.assertEqual(loaded.mutation_journal[0].state, "pending")
            loaded.append_mutation_journal(MutationJournalEntry(
                key_hash="e" * 64, method="POST", path="/api/config", request_hash="f" * 64,
            ))
            save_config(loaded, path)
            self.assertIn(pending.id, [entry.id for entry in load_config(path).mutation_journal])

    def test_save_cannot_publish_an_unloadable_oversized_journal(self) -> None:
        cfg = AppConfig()
        cfg.mutation_journal = [MutationJournalEntry(
            key_hash="a" * 64, method="POST", path="/api/live/order", request_hash="b" * 64,
            live=True,
        ) for _ in range(MAX_MUTATION_JOURNAL_ENTRIES + 1)]
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "new-state"
            with self.assertRaises(ValueError):
                save_config(cfg, parent / "config.json")
            self.assertFalse(parent.exists())

    def test_legacy_file_without_new_journals_roundtrips_with_finite_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"wallets":[],"copytrading":{"copy_percentage":25},"markets":{"polymarket":{"settings":{"limit":1e3}}}}', encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.mutation_journal, [])
            self.assertEqual(cfg.copy_activity_outbox, [])
            self.assertEqual(cfg.copytrading.scale, 0.25)
            self.assertEqual(cfg.markets["polymarket"].settings["limit"], 1000.0)
            save_config(cfg, path)
            self.assertEqual(load_config(path).copytrading.scale, 0.25)

    def test_inaccessible_existing_config_is_never_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch.object(Path, "exists", return_value=False),
                patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")),
                self.assertRaises(ConfigLoadError),
            ):
                load_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")

    def test_new_writer_cannot_replace_unreadable_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with (
                patch.object(Path, "exists", return_value=False),
                patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")),
                patch("core.storage.replace_file") as replace,
            ):
                with self.assertRaises(PermissionError):
                    save_config(AppConfig(), path)
                replace.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")

    def test_nonregular_configuration_is_rejected_before_opening(self) -> None:
        for mode in (S_IFDIR, S_IFIFO, S_IFLNK):
            with (
                self.subTest(mode=mode),
                patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode)),
                patch.object(Path, "read_bytes") as read,
                self.assertRaises(ConfigLoadError),
            ):
                load_config(Path("nonregular-config"))
            read.assert_not_called()

    def test_default_config_path_can_use_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "installed" / "config.json"
            with patch.dict("core.storage.os.environ", {CONFIG_PATH_ENV: str(path)}):
                self.assertEqual(default_config_path(), path)

    def test_save_config_does_not_clobber_existing_file_when_atomic_replace_fails(self) -> None:
        original = AppConfig(theme="dark", selected_market_id="kalshi")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(original, path)
            replacement = load_config(path)
            replacement.theme = "light"
            replacement.selected_market_id = "polymarket"

            with patch("core.storage.os.replace", side_effect=OSError("disk full super-secret-token")):
                with self.assertRaises(OSError):
                    save_config(replacement, path)

            loaded = load_config(path)
            leftovers = list(Path(temp_dir).glob(".config.json.*.tmp"))

        self.assertEqual(loaded.theme, "dark")
        self.assertEqual(loaded.ui_design, "aurora_2026")
        self.assertEqual(loaded.selected_market_id, "kalshi")
        self.assertEqual(leftovers, [])

    def test_new_config_can_create_missing_file_and_cannot_overwrite_existing_file(self) -> None:
        creator = AppConfig(theme="dark", selected_market_id="kalshi")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(creator, path)
            creator.theme = "light"
            save_config(creator, path)

            untracked = AppConfig(theme="dark", selected_market_id="polymarket")
            with self.assertRaisesRegex(ConfigConflictError, "Reload it before saving"):
                save_config(untracked, path)

            stored = load_config(path)

        self.assertEqual(stored.theme, "light")
        self.assertEqual(stored.selected_market_id, "kalshi")

    def test_stale_config_writer_cannot_overwrite_newer_process_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(AppConfig(), path)
            first_writer = load_config(path)
            stale_writer = load_config(path)

            first_writer.theme = "dark"
            save_config(first_writer, path)
            stale_writer.selected_market_id = "kalshi"
            with self.assertRaisesRegex(ConfigConflictError, "changed in another process"):
                save_config(stale_writer, path)

            stored = load_config(path)

        self.assertEqual(stored.theme, "dark")
        self.assertEqual(stored.selected_market_id, "polymarket")

    def test_concurrent_saves_of_shared_config_serialize_snapshot_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(AppConfig(), path)
            shared = load_config(path)
            first_snapshotted = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()
            second_snapshotted = threading.Event()
            errors: list[BaseException] = []
            original_to_dict = AppConfig.to_dict

            def instrumented_to_dict(instance: AppConfig) -> dict:
                snapshot = original_to_dict(instance)
                if threading.current_thread().name == "config-save-first":
                    first_snapshotted.set()
                    if not release_first.wait(timeout=5.0):
                        raise TimeoutError("first config save was not released")
                elif threading.current_thread().name == "config-save-second":
                    second_snapshotted.set()
                return snapshot

            def save_first() -> None:
                try:
                    save_config(shared, path)
                except BaseException as exc:
                    errors.append(exc)

            def save_second() -> None:
                second_started.set()
                try:
                    save_config(shared, path)
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=save_first, name="config-save-first")
            second = threading.Thread(target=save_second, name="config-save-second")
            second_entered_during_first_save = False
            with patch.object(AppConfig, "to_dict", instrumented_to_dict):
                shared.theme = "dark"
                first.start()
                try:
                    self.assertTrue(first_snapshotted.wait(timeout=2.0))
                    shared.theme = "light"
                    shared.selected_market_id = "kalshi"
                    second.start()
                    self.assertTrue(second_started.wait(timeout=2.0))
                    second_entered_during_first_save = second_snapshotted.wait(timeout=0.5)
                finally:
                    release_first.set()
                    first.join(timeout=5.0)
                    if second.ident is not None:
                        second.join(timeout=5.0)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(second_entered_during_first_save)
            self.assertEqual(errors, [])
            stored = load_config(path)

        self.assertEqual(stored.theme, "light")
        self.assertEqual(stored.selected_market_id, "kalshi")

    def test_committed_config_can_be_retried_after_parent_fsync_failure(self) -> None:
        cfg = AppConfig(theme="dark")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            with patch("core.storage._fsync_parent_directory", side_effect=OSError("directory sync failed")):
                with self.assertRaisesRegex(OSError, "directory sync failed"):
                    save_config(cfg, path)

            committed = load_config(path)
            cfg.theme = "light"
            save_config(cfg, path)
            stored = load_config(path)

        self.assertEqual(committed.theme, "dark")
        self.assertEqual(stored.theme, "light")

    def test_configuration_parent_directory_is_synced_on_posix(self) -> None:
        path = Path("config") / "config.json"
        with (
            patch("core.storage.os.name", "posix"),
            patch("core.storage.os.open", return_value=42) as open_directory,
            patch("core.storage.os.fsync") as sync,
            patch("core.storage.os.close") as close,
        ):
            _fsync_parent_directory(path)

        open_directory.assert_called_once()
        sync.assert_called_once_with(42)
        close.assert_called_once_with(42)

    def test_unknown_selected_market_falls_back_to_polymarket(self) -> None:
        loaded = AppConfig.from_dict({"selected_market_id": "unknown-market"})

        self.assertEqual(loaded.selected_market_id, "polymarket")

    def test_legacy_alert_without_market_id_defaults_to_polymarket(self) -> None:
        alert = PriceAlert.from_dict(
            {
                "token_id": "token-1",
                "label": "Legacy alert",
                "direction": "above",
                "threshold": 0.5,
            }
        )

        self.assertEqual(alert.market_id, "polymarket")

    def test_paper_trade_record_normalizes_loaded_values(self) -> None:
        record = PaperTradeRecord.from_dict(
            {
                "market_id": "KALSHI",
                "contract_id": "FED-YES:yes",
                "side": "buy",
                "size": "2",
                "limit_price": "",
                "accepted": True,
                "message": "DRY RUN",
            }
        )

        self.assertEqual(record.market_id, "kalshi")
        self.assertEqual(record.side, "BUY")
        self.assertEqual(record.size, 2.0)
        self.assertIsNone(record.limit_price)


if __name__ == "__main__":
    unittest.main()
