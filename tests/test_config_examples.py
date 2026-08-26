from __future__ import annotations

import json
import unittest
from pathlib import Path

from core.models import AppConfig
from market_adapters import MARKET_IDS
from market_adapters.catalog import MARKET_CATALOG
from market_adapters.registry import VERIFIED_BLOCKERS


ROOT = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = ROOT / "data" / "config.example.json"
ENV_EXAMPLE = ROOT / ".env.example"
FORBIDDEN_DIRECT_SECRET_KEYS = {
    "api_key",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
}


class ConfigExampleTests(unittest.TestCase):
    def test_config_example_parses_and_covers_all_catalog_markets(self) -> None:
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        cfg = AppConfig.from_dict(data)

        self.assertEqual(set(data["markets"]), set(MARKET_IDS))
        self.assertEqual(set(cfg.markets), set(MARKET_IDS))
        self.assertEqual(cfg.selected_market_id, "polymarket")
        self.assertEqual(cfg.ui_design, "aurora_2026")
        self.assertTrue(cfg.markets["polymarket"].enabled)
        self.assertFalse(cfg.copytrading.enabled)
        self.assertFalse(cfg.copytrading.live)

    def test_config_example_disables_live_trading_for_every_market(self) -> None:
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))

        for market_id, market_cfg in data["markets"].items():
            settings = market_cfg.get("settings") or {}
            self.assertFalse(settings.get("live_trading_enabled"), market_id)

    def test_config_example_status_matches_registry_capabilities(self) -> None:
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))

        for market in MARKET_CATALOG:
            settings = data["markets"][market.market_id].get("settings") or {}
            expected = "verified_blocked" if market.market_id in VERIFIED_BLOCKERS else "implemented"
            self.assertEqual(settings.get("adapter_status"), expected, market.market_id)

    def test_config_example_does_not_store_direct_secret_fields(self) -> None:
        data = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))

        for market_id, market_cfg in data["markets"].items():
            settings = market_cfg.get("settings") or {}
            direct_secret_keys = FORBIDDEN_DIRECT_SECRET_KEYS.intersection(settings)
            self.assertEqual(direct_secret_keys, set(), market_id)

    def test_config_and_docs_do_not_use_tbd_placeholders(self) -> None:
        paths = [
            CONFIG_EXAMPLE,
            ROOT / "README.md",
            ROOT / "docs" / "BLOCKERS.md",
        ]

        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotIn("TBD", path.read_text(encoding="utf-8"))

    def test_env_example_uses_empty_placeholders(self) -> None:
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        assignments = [line for line in lines if line and not line.startswith("#") and "=" in line]

        for assignment in assignments:
            name, value = assignment.split("=", 1)
            self.assertTrue(name)
            self.assertEqual(value, "", name)


if __name__ == "__main__":
    unittest.main()
