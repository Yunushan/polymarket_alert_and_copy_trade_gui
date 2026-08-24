from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import CryptoComPredictAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError, MarketHTTPError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crypto_com_predict"
EVENT_ID = "7aab6b9f-38b7-4b54-a14a-0b32b83d3348"
CONTRACT_SYMBOL = "ELECT-CA-GOV-2026-DEM"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class CryptoComPredictAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        adapter = CryptoComPredictAdapter(config)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/events/search") or url.endswith("/events"):
                return load_fixture("events")
            if url.endswith(f"/events/{EVENT_ID}/contracts"):
                return load_fixture("contracts")
            if url.endswith(f"/contracts/{CONTRACT_SYMBOL}/price"):
                return load_fixture("price")
            raise AssertionError(f"unexpected Crypto.com Predictions URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_metadata_advertises_only_supported_market_data_features(self) -> None:
        adapter = CryptoComPredictAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "crypto_com_predict")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertTrue(health["anonymous_read_access"])
        self.assertFalse(health["api_key_configured"])
        self.assertEqual(health["api_key_source"], "anonymous")
        self.assertEqual(health["runtime"]["min_request_interval_seconds"], 0.6)
        self.assertIn("data-api.crypto.com", health["api_base_url"])
        self.assertIn("Market Data License", health["license_notice"])
        self.assertTrue(health["orderbook_supported"])
        self.assertFalse(health["orderbook_depth_supported"])

    def test_list_and_search_events_use_documented_endpoints(self) -> None:
        adapter, calls = self.make_adapter()

        events = adapter.list_events("governor", limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, EVENT_ID)
        self.assertEqual(events[0].status, "active")
        self.assertIn("California", events[0].title)
        self.assertTrue(calls[-1][0].endswith("/events/search"))
        self.assertEqual(calls[-1][1], {"limit": 10, "q": "governor"})

        adapter.list_events(limit=500)
        self.assertTrue(calls[-1][0].endswith("/events"))
        self.assertEqual(calls[-1][1], {"limit": 100})

    def test_list_contracts_and_get_price_map_official_payloads(self) -> None:
        adapter, _ = self.make_adapter()

        contracts = adapter.list_contracts(EVENT_ID)
        price = adapter.get_price(CONTRACT_SYMBOL)

        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].contract_id, CONTRACT_SYMBOL)
        self.assertEqual(contracts[0].event_id, EVENT_ID)
        self.assertEqual(contracts[0].outcome, "Democratic")
        self.assertEqual(price.contract_id, CONTRACT_SYMBOL)
        self.assertEqual(price.bid, 0.43)
        self.assertEqual(price.ask, 0.63)
        self.assertEqual(price.midpoint, 0.53)
        self.assertEqual(price.last, 0.63)
        self.assertEqual(price.source, "crypto_com_predictions_market_data")

    def test_optional_licensed_api_key_is_sent_but_not_exposed(self) -> None:
        adapter, calls = self.make_adapter({"crypto_com_predict_api_key": "licensed-test-key"})

        adapter.list_events(limit=1)
        health = adapter.health_check()

        self.assertEqual(calls[-1][2], {"X-API-Key": "licensed-test-key"})
        self.assertTrue(health["api_key_configured"])
        self.assertEqual(health["api_key_source"], "config:crypto_com_predict_api_key")
        self.assertNotIn("licensed-test-key", str(health))

    def test_paper_order_is_dry_run_and_validates_inputs(self) -> None:
        adapter, _ = self.make_adapter()
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="crypto_com_predict",
                contract_id=CONTRACT_SYMBOL,
                side="BUY",
                size=5,
                limit_price=0.55,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.filled_size, 0.0)
        self.assertIn("DRY RUN", result.message)
        self.assertTrue(result.raw["official_api_is_read_only"])

        for order in (
            PaperOrderRequest("other", CONTRACT_SYMBOL, "BUY", 1, 0.5),
            PaperOrderRequest("crypto_com_predict", "", "BUY", 1, 0.5),
            PaperOrderRequest("crypto_com_predict", CONTRACT_SYMBOL, "HOLD", 1, 0.5),
            PaperOrderRequest("crypto_com_predict", CONTRACT_SYMBOL, "BUY", 0, 0.5),
            PaperOrderRequest("crypto_com_predict", CONTRACT_SYMBOL, "BUY", 1, 100),
            PaperOrderRequest("crypto_com_predict", CONTRACT_SYMBOL, "BUY", 1, 101),
        ):
            with self.subTest(order=order):
                with self.assertRaises(MarketConfigurationError):
                    adapter.place_paper_order(order)

    def test_top_of_book_and_unsupported_execution_fail_clearly(self) -> None:
        adapter, _ = self.make_adapter()

        book = adapter.get_orderbook(CONTRACT_SYMBOL)
        self.assertEqual([(level.price, level.size) for level in book.bids], [(0.43, 0.0)])
        self.assertEqual([(level.price, level.size) for level in book.asks], [(0.63, 0.0)])
        self.assertEqual(book.raw["depth"], "top_of_book_only")
        self.assertFalse(book.raw["size_available"])

        with self.assertRaises(UnsupportedFeatureError) as live_ctx:
            adapter.place_live_order(
                PaperOrderRequest("crypto_com_predict", CONTRACT_SYMBOL, "BUY", 1, 0.5)
            )
        self.assertEqual(live_ctx.exception.feature, "live_trading")

        with self.assertRaises(UnsupportedFeatureError) as copy_ctx:
            adapter.copy_trade_from_activity({})
        self.assertEqual(copy_ctx.exception.feature, "copy_trading")

        adapter.runtime.get_json = lambda *args, **kwargs: {"data": {}}  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.get_price(CONTRACT_SYMBOL)

        adapter.runtime.get_json = lambda *args, **kwargs: {}  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.get_price(CONTRACT_SYMBOL)


if __name__ == "__main__":
    unittest.main()
