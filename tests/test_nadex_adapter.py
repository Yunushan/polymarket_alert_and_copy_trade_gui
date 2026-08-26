from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import NadexAdapter, PaperOrderRequest, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nadex"
EVENT_ID = "nadex-cdna-sports-2026"
CONTRACT_SYMBOL = "NADEX-CDNA-US500-YES"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class NadexAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        adapter = NadexAdapter(config)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/events/search") or url.endswith("/events"):
                return load_fixture("events")
            if url.endswith(f"/events/{EVENT_ID}/contracts"):
                return load_fixture("contracts")
            if url.endswith(f"/contracts/{CONTRACT_SYMBOL}/price"):
                return load_fixture("price")
            raise AssertionError(f"unexpected Nadex/CDNA URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_metadata_is_read_only_cdna_alias(self) -> None:
        adapter = NadexAdapter()
        health = adapter.health_check()

        self.assertEqual(adapter.market_id, "nadex")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.kyc_required)
        self.assertTrue(adapter.capabilities.region_limited)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertEqual(health["alias_of"], "crypto_com_predict")
        self.assertEqual(
            health["underlying_market_data_provider"],
            "Crypto.com Derivatives North America (CDNA/Nadex)",
        )
        self.assertEqual(health["supported_public_data_scope"], "CDNA/Nadex prediction-event market data only")
        self.assertIn("https://www.nadex.com/product-market/", health["references"])
        self.assertIn("https://data-api.crypto.com/docs", health["references"])
        self.assertFalse(health["nadex_order_api_supported"])
        self.assertFalse(health["dcm_fix_api_supported"])
        self.assertFalse(health["knockout_contracts_supported"])
        self.assertIn("Market Data License", health["license_notice"])

    def test_alias_uses_cdna_endpoints_and_maps_payloads(self) -> None:
        adapter, calls = self.make_adapter({"nadex_api_base_url": "https://fixture.invalid/api"})

        events = adapter.list_events("reference", limit=10)
        contracts = adapter.list_contracts(EVENT_ID)
        price = adapter.get_price(CONTRACT_SYMBOL)

        self.assertEqual(events[0].market_id, "nadex")
        self.assertEqual(events[0].url, "https://www.nadex.com/product-market/")
        self.assertEqual(contracts[0].contract_id, "NADEX-CDNA-US500-YES")
        self.assertEqual(contracts[0].url, "https://www.nadex.com/product-market/")
        self.assertEqual(price.market_id, "nadex")
        self.assertEqual(price.midpoint, 0.5)
        self.assertEqual(price.source, "nadex_cdna_predictions_market_data")
        self.assertTrue(all(url.startswith("https://fixture.invalid/api/") for url, _, _ in calls))

    def test_api_key_and_paper_order_are_scoped_to_alias(self) -> None:
        adapter, calls = self.make_adapter({"nadex_api_key": "nadex-test-key"})
        adapter.list_events(limit=1)
        self.assertEqual(calls[-1][2], {"X-API-Key": "nadex-test-key"})
        self.assertEqual(adapter.health_check()["api_key_source"], "config:nadex_api_key")

        result = adapter.place_paper_order(PaperOrderRequest("nadex", CONTRACT_SYMBOL, "BUY", 2, 0.5))
        self.assertTrue(result.accepted)
        self.assertIn("Nadex/CDNA Predictions", result.message)
        self.assertEqual(result.filled_size, 0.0)

    def test_top_of_book_is_available_but_live_and_copy_remain_unsupported(self) -> None:
        adapter, _ = self.make_adapter()

        book = adapter.get_orderbook(CONTRACT_SYMBOL)
        self.assertEqual([(level.price, level.size) for level in book.bids], [(0.41, 0.0)])
        self.assertEqual([(level.price, level.size) for level in book.asks], [(0.59, 0.0)])
        self.assertEqual(book.raw["depth"], "top_of_book_only")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("nadex", CONTRACT_SYMBOL, "BUY", 1, 0.5))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
