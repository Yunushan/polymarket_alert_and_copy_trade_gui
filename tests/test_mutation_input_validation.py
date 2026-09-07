from __future__ import annotations

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.models import AppConfig, MarketConfig
from core.storage import ConfigLoadError, load_config, save_config
import market_sentinel_cli
import test_web_api
from web_api import apply_copy_settings_patch, apply_market_patch, optional_positive_float


WALLET = "0x" + "1" * 40
OTHER_WALLET = "0x" + "2" * 40
MARKET_FLAGS = (
    "live_trading_enabled", "live_trading_confirmed", "live_trading_acknowledged",
    "live_trading_kill_switch", "live_trading_paused", "copy_trading_enabled",
)
INVALID_FLAGS = ("false", "true", "invalid", "", 0, 1, None, [], ["false"], {})
INVALID_JSON = (
    '{"live":false,"live":true}',
    '{"live":false,"l\\u0069ve":true}',
    '{"settings":{"live_trading_enabled":false,"live_trading_enabled":true}}',
    '{"ignored":NaN}', '{"ignored":Infinity}', '{"ignored":-Infinity}',
    '{"ignored":1e999}',
)


class MutationInputValidationTests(unittest.TestCase):
    def test_market_patch_rejects_invalid_flags_before_mutating(self) -> None:
        for field in ("enabled", *MARKET_FLAGS):
            for value in INVALID_FLAGS:
                payloads = [{field: value}]
                if field != "enabled":
                    payloads.append({"enabled": True, "settings": {field: value}})
                for payload in payloads:
                    with self.subTest(payload=payload):
                        cfg = AppConfig()
                        before = cfg.to_dict()
                        with self.assertRaises(ValueError):
                            apply_market_patch(cfg, "kalshi", payload)
                        self.assertEqual(cfg.to_dict(), before)

    def test_market_settings_are_validated_on_load_and_save(self) -> None:
        for field in MARKET_FLAGS:
            for value in INVALID_FLAGS:
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    raw = json.dumps({"markets": {"kalshi": {"settings": {field: value}}}}).encode()
                    path.write_bytes(raw)
                    with self.assertRaises(ConfigLoadError):
                        load_config(path)
                    self.assertEqual(path.read_bytes(), raw)
                    cfg = AppConfig()
                    cfg.markets["kalshi"].settings[field] = value
                    target = Path(directory) / "new.json"
                    with self.assertRaises(ValueError):
                        save_config(cfg, target)
                    self.assertFalse(target.exists())

    def test_market_caps_reject_invalid_raw_types_and_values(self) -> None:
        for field in ("live_trading_max_size", "live_trading_max_notional"):
            for value in (True, False, [], {}, -1, 0, "invalid", "NaN", "Infinity", float("nan"), float("inf")):
                for nested in (False, True):
                    with self.subTest(field=field, value=value, nested=nested):
                        cfg = AppConfig()
                        before = cfg.to_dict()
                        payload = {"settings": {field: value}} if nested else {field: value}
                        with self.assertRaises(ValueError):
                            apply_market_patch(cfg, "kalshi", payload)
                        self.assertEqual(cfg.to_dict(), before)

    def test_market_alias_conflicts_fail_but_equivalent_values_and_clearing_work(self) -> None:
        for field, first, second in (
            ("live_trading_enabled", False, True),
            ("live_trading_confirmed", False, True),
            ("live_trading_kill_switch", True, False),
            ("live_trading_max_size", "2", 3),
            ("live_trading_max_notional", "", 10),
        ):
            with self.subTest(field=field):
                cfg = AppConfig()
                before = cfg.to_dict()
                with self.assertRaises(ValueError):
                    apply_market_patch(cfg, "kalshi", {field: first, "settings": {field: second}})
                self.assertEqual(cfg.to_dict(), before)
        cfg = AppConfig()
        apply_market_patch(cfg, "kalshi", {
            "enabled": True, "live_trading_enabled": False, "live_trading_max_size": "2.5",
            "settings": {"live_trading_enabled": False, "live_trading_max_size": 2.5, "custom_list": [1, "x"]},
        })
        self.assertIs(cfg.markets["kalshi"].settings["live_trading_enabled"], False)
        self.assertEqual(cfg.markets["kalshi"].settings["live_trading_max_size"], 2.5)
        self.assertEqual(cfg.markets["kalshi"].settings["custom_list"], [1, "x"])
        apply_market_patch(cfg, "kalshi", {"live_trading_max_size": None, "settings": {"live_trading_max_size": " "}})
        self.assertNotIn("live_trading_max_size", cfg.markets["kalshi"].settings)
        with self.assertRaises(ValueError):
            MarketConfig.from_dict("kalshi", {"settings": []})

    def test_copy_flags_reject_invalid_inputs_without_mutation(self) -> None:
        for field in ("enabled", "live", "allow_sells", "conflict_guard"):
            for value in INVALID_FLAGS:
                with self.subTest(field=field, value=value):
                    cfg = AppConfig()
                    before = cfg.copytrading.to_dict()
                    with self.assertRaises(ValueError):
                        apply_copy_settings_patch(cfg, {field: value})
                    self.assertEqual(cfg.copytrading.to_dict(), before)

    def test_copy_numbers_are_not_defaulted_clamped_or_truncated(self) -> None:
        for field in ("scale", "copy_percentage", "percentage", "scale_percent", "slippage", "max_usdc_per_trade", "conflict_window_seconds"):
            for value in (True, False, None, "", "invalid", "NaN", "Infinity", [], {}, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    cfg = AppConfig()
                    before = cfg.copytrading.to_dict()
                    with self.assertRaises(ValueError):
                        apply_copy_settings_patch(cfg, {field: value})
                    self.assertEqual(cfg.copytrading.to_dict(), before)
        for value in (0.9, "0.9", -1, 86401):
            with self.subTest(window=value), self.assertRaises(ValueError):
                apply_copy_settings_patch(AppConfig(), {"conflict_window_seconds": value})

    def test_copy_percentage_aliases_must_agree(self) -> None:
        for payload in (
            {"copy_percentage": 100, "scale": 0},
            {"copy_percentage": 100, "percentage": 0},
            {"scale_percent": 100, "percentage": 0},
            {"percentage": 25, "scale": 0.75},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                apply_copy_settings_patch(AppConfig(), payload)
        for percentage in (0, 25, 100):
            cfg = AppConfig()
            value = apply_copy_settings_patch(cfg, {
                "copy_percentage": str(percentage), "percentage": percentage,
                "scale_percent": percentage, "scale": percentage / 100,
                "conflict_window_seconds": "86400", "max_usdc_per_trade": "2.5",
                "slippage": "0", "live": False,
            })
            self.assertEqual(value.scale, percentage / 100)
            self.assertEqual(value.max_usdc_per_trade, 2.5)
            self.assertEqual(value.conflict_window_seconds, 86400)
            self.assertEqual(apply_copy_settings_patch(cfg, {}).to_dict(), value.to_dict())

    def test_wallet_inputs_do_not_silently_clear_or_override_other_aliases(self) -> None:
        for payload in (
            {"follow_wallet": None}, {"follow_wallet": False}, {"follow_wallet": []},
            {"follow_wallets": None}, {"follow_wallets": [None]}, {"follow_wallets": [False]},
            {"follow_wallets": [WALLET], "follow_wallet": OTHER_WALLET},
            {"follow_wallets": [WALLET], "follow_wallet": {}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                apply_copy_settings_patch(AppConfig(), payload)
        cfg = AppConfig()
        apply_copy_settings_patch(cfg, {"follow_wallet": WALLET, "follow_wallets": [WALLET, OTHER_WALLET]})
        self.assertEqual(cfg.copytrading.normalized_follow_wallets(), [WALLET, OTHER_WALLET])
        apply_copy_settings_patch(cfg, {"follow_wallets": []})
        self.assertEqual(cfg.copytrading.normalized_follow_wallets(), [])

    def test_optional_positive_numbers_reject_boolean_and_nonfinite_values(self) -> None:
        for value in (True, False, float("nan"), float("inf"), "NaN", "Infinity", 10 ** 1000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                optional_positive_float(value, "Order size")
        self.assertEqual(optional_positive_float("2.5", "Order size"), 2.5)
        self.assertIsNone(optional_positive_float(" ", "Order size"))

    def test_http_rejected_mutations_preserve_exact_stored_bytes(self) -> None:
        helper = test_web_api.WebApiTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            save_config(AppConfig(), path)
            frontend = root / "frontend"
            frontend.mkdir()
            server, thread, origin = helper._serve_api(path, frontend, api_token="test-only")
            try:
                cases = [
                    ("/api/markets/kalshi", '{"enabled":"false","live_trading_enabled":"false"}'),
                    ("/api/markets/kalshi", '{"settings":{"live_trading_confirmed":1}}'),
                    ("/api/copy", '{"live":["false"],"scale":true}'),
                    ("/api/copy", '{"conflict_window_seconds":0.9}'),
                    ("/api/copy", '{"copy_percentage":100,"scale":0}'),
                    *(("/api/copy", raw) for raw in INVALID_JSON),
                ]
                for route, raw in cases:
                    with self.subTest(route=route, raw=raw):
                        before = path.read_bytes()
                        status, _ = helper._request_json(origin, route, method="PATCH", raw=raw.encode(), headers={"Authorization": "Bearer test-only"})
                        self.assertEqual(status, 400)
                        self.assertEqual(path.read_bytes(), before)
                for route, payload in (
                    ("/api/markets/kalshi", {"enabled": True, "live_trading_enabled": False}),
                    ("/api/copy", {"live": False, "copy_percentage": "25", "conflict_window_seconds": 0}),
                ):
                    status, _ = helper._request_json(origin, route, method="PATCH", payload=payload, headers={"Authorization": "Bearer test-only"})
                    self.assertEqual(status, 200)
                loaded = load_config(path)
                self.assertFalse(loaded.markets["kalshi"].settings["live_trading_enabled"])
                self.assertFalse(loaded.copytrading.live)
                self.assertEqual(loaded.copytrading.scale, 0.25)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def test_cli_rejected_json_preserves_state_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, output = root / "config.json", root / "output.json"
            save_config(AppConfig(), path)
            output.write_text("previous export", encoding="utf-8")
            cases = [
                (["markets", "set", "kalshi"], '{"live_trading_enabled":"false"}'),
                (["copy", "set"], '{"live":["false"]}'),
                (["copy", "set"], '{"scale":true}'),
                (["copy", "set"], '{"conflict_window_seconds":0.9}'),
                (["copy", "set"], '{"percentage":0,"copy_percentage":100}'),
                *((["copy", "set"], raw) for raw in INVALID_JSON),
            ]
            for prefix, raw in cases:
                with self.subTest(prefix=prefix, raw=raw), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    before = path.read_bytes()
                    result = market_sentinel_cli.main([*prefix, "--config", str(path), "--json", raw, "--output", str(output)])
                    self.assertNotEqual(result, 0)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(output.read_text(encoding="utf-8"), "previous export")

    def test_cli_explicit_flags_and_numeric_strings_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            path = Path(directory) / "config.json"
            result = market_sentinel_cli.main([
                "copy", "set", "--config", str(path), "--enabled", "--no-live",
                "--follow-wallet", WALLET, "--copy-percentage", "25",
                "--conflict-window-seconds", "300", "--max-usdc-per-trade", "2.5",
            ])
            self.assertEqual(result, 0)
            cfg = load_config(path)
            self.assertFalse(cfg.copytrading.live)
            self.assertEqual(cfg.copytrading.scale, 0.25)
            self.assertEqual(cfg.copytrading.max_usdc_per_trade, 2.5)
            before = deepcopy(cfg.to_dict())
            self.assertEqual(load_config(path).to_dict(), before)

    def test_cli_json_files_and_setting_values_use_strict_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, source = Path(directory) / "config.json", Path(directory) / "input.json"
            save_config(AppConfig(), path)
            before = path.read_bytes()
            for raw in INVALID_JSON:
                source.write_text(raw, encoding="utf-8")
                for args in (
                    ["copy", "set", "--json", "@" + str(source)],
                    ["markets", "set", "kalshi", "--setting", "custom=" + raw],
                ):
                    with self.subTest(args=args), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                        self.assertNotEqual(market_sentinel_cli.main([*args, "--config", str(path)]), 0)
                        self.assertEqual(path.read_bytes(), before)
            with patch("sys.stdout", io.StringIO()):
                self.assertEqual(market_sentinel_cli.main([
                    "markets", "set", "kalshi", "--config", str(path),
                    "--setting", "live_trading_enabled=false", "--setting", "custom=ordinary-text",
                ]), 0)
            self.assertIs(load_config(path).markets["kalshi"].settings["live_trading_enabled"], False)
            self.assertEqual(load_config(path).markets["kalshi"].settings["custom"], "ordinary-text")


if __name__ == "__main__":
    unittest.main()
