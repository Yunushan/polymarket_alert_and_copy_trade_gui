from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import FanaticsMarketsAdapter, PaperOrderRequest, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fanatics_markets"
EVENT_ID = "fanatics-cdn-a1"
CONTRACT_SYMBOL = "FANATICS-CHAMP-YES"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FanaticsMarketsAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        adapter = FanaticsMarketsAdapter(config)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/events/search") or url.endswith("/events"):
                return load_fixture("events")
            if url.endswith(f"/events/{EVENT_ID}/contracts"):
                return load_fixture("contracts")
            if url.endswith(f"/contracts/{CONTRACT_SYMBOL}/price"):
                return load_fixture("price")
            raise AssertionError(f"unexpected Fanatics/CDNA URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_metadata_is_read_only_alias_with_region_and_kyc_limits(self) -> None:
        adapter = FanaticsMarketsAdapter()
        health = adapter.health_check()

        self.assertEqual(adapter.market_id, "fanatics_markets")
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
        self.assertEqual(health["intermediary"], "Fanatics Markets / Paragon Global Markets")
        self.assertIn("https://data.crypto.com/docs", health["references"])
        self.assertFalse(health["fanatics_order_api_supported"])
        self.assertIn("Market Data License", health["license_notice"])

    def test_alias_uses_cdna_endpoints_and_maps_payloads(self) -> None:
        adapter, calls = self.make_adapter({"fanatics_markets_api_base_url": "https://fixture.invalid/api"})

        events = adapter.list_events("championship", limit=10)
        contracts = adapter.list_contracts(EVENT_ID)
        price = adapter.get_price(CONTRACT_SYMBOL)

        self.assertEqual(events[0].market_id, "fanatics_markets")
        self.assertEqual(events[0].url, "https://fanaticsmarkets.com")
        self.assertEqual(contracts[0].contract_id, CONTRACT_SYMBOL)
        self.assertEqual(contracts[0].url, "https://fanaticsmarkets.com")
        self.assertEqual(price.market_id, "fanatics_markets")
        self.assertEqual(price.midpoint, 0.5)
        self.assertEqual(price.source, "cdna_predictions_market_data")
        self.assertTrue(all(url.startswith("https://fixture.invalid/api/") for url, _, _ in calls))

    def test_api_key_and_paper_order_are_scoped_to_alias(self) -> None:
        adapter, calls = self.make_adapter({"fanatics_markets_api_key": "fanatics-test-key"})
        adapter.list_events(limit=1)
        self.assertEqual(calls[-1][2], {"X-API-Key": "fanatics-test-key"})
        self.assertEqual(adapter.health_check()["api_key_source"], "config:fanatics_markets_api_key")

        result = adapter.place_paper_order(
            PaperOrderRequest("fanatics_markets", CONTRACT_SYMBOL, "BUY", 2, 0.5)
        )
        self.assertTrue(result.accepted)
        self.assertIn("Fanatics Markets/CDNA", result.message)
        self.assertEqual(result.filled_size, 0.0)

    def test_top_of_book_is_available_but_live_and_copy_remain_unsupported(self) -> None:
        adapter, _ = self.make_adapter()

        book = adapter.get_orderbook(CONTRACT_SYMBOL)
        self.assertEqual([(level.price, level.size) for level in book.bids], [(0.42, 0.0)])
        self.assertEqual([(level.price, level.size) for level in book.asks], [(0.58, 0.0)])
        self.assertEqual(book.raw["depth"], "top_of_book_only")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("fanatics_markets", CONTRACT_SYMBOL, "BUY", 1, 0.5))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
