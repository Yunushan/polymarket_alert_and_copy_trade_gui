from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import PaperOrderRequest, SpaceAdapter, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "space"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class SpaceAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        settings = {"space_market_status": "active"}
        settings.update(config or {})
        adapter = SpaceAdapter(settings)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            self.assertEqual(headers, {})
            if url.endswith("/markets"):
                return load_fixture("markets")
            if url.endswith("/markets/btc-150k-2025/orderbook"):
                self.assertEqual(params.get("outcome"), "YES")
                return load_fixture("orderbook")
            if url.endswith("/markets/btc-150k-2025/trades"):
                self.assertEqual(params.get("outcome"), "YES")
                return load_fixture("trades")
            if url.endswith("/markets/btc-150k-2025/candles"):
                self.assertEqual(params.get("outcome"), "YES")
                return load_fixture("candles")
            if url.endswith("/markets/btc-150k-2025"):
                return load_fixture("market")
            raise AssertionError(f"unexpected Space URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_health_and_documented_capabilities_are_explicit(self) -> None:
        adapter, _ = self.make_adapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "space")
        self.assertTrue(health["public_api"])
        self.assertTrue(health["anonymous_read_access"])
        self.assertIn("public production release", health["production_api_notice"])
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)

    def test_events_contracts_prices_orderbook_and_paper_order(self) -> None:
        adapter, calls = self.make_adapter()

        events = adapter.list_events("btc")
        contracts = adapter.list_contracts(events[0].event_id)
        yes_price = adapter.get_price("btc-150k-2025:YES")
        no_price = adapter.get_price("btc-150k-2025:NO")
        orderbook = adapter.get_orderbook("btc-150k-2025:YES")
        paper = adapter.place_paper_order(
            PaperOrderRequest("space", "btc-150k-2025:YES", "BUY", 2, 0.35)
        )

        self.assertEqual(events[0].event_id, "space:btc-150k-2025")
        self.assertEqual(events[0].status, "active")
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        self.assertAlmostEqual(yes_price.last or 0.0, 0.35)
        self.assertAlmostEqual(no_price.last or 0.0, 0.65)
        self.assertEqual(yes_price.source, "space_rest_market_detail")
        self.assertEqual([level.price for level in orderbook.bids], [0.349, 0.348])
        self.assertEqual([level.price for level in orderbook.asks], [0.351, 0.352])
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.35)
        self.assertGreaterEqual(len(calls), 3)

    def test_public_trade_and_candle_history_is_normalized(self) -> None:
        adapter, calls = self.make_adapter()

        trades = adapter.list_trades("btc-150k-2025:YES", limit=2, after=1704150000)
        candles = adapter.list_candles(
            "btc-150k-2025:YES",
            resolution="15m",
            from_timestamp=1704150000,
            to_timestamp=1704153600,
        )

        self.assertEqual([trade.trade_id for trade in trades], ["trade_abc123", "trade_def456"])
        self.assertEqual([trade.side for trade in trades], ["BUY", "SELL"])
        self.assertAlmostEqual(trades[0].price, 0.35)
        self.assertEqual(trades[0].size, 1000.0)
        self.assertEqual(candles[0].timestamp, 1704153600.0)
        self.assertAlmostEqual(candles[0].close, 0.351)
        self.assertEqual(candles[0].volume, 125000.0)
        self.assertTrue(any(url.endswith("/trades") for url, _, _ in calls))
        self.assertTrue(any(url.endswith("/candles") for url, _, _ in calls))

    def test_multi_outcome_payload_and_query_filter_are_supported(self) -> None:
        adapter, _ = self.make_adapter()
        market = load_fixture("market")
        market["outcomes"] = [{"name": "Candidate A"}, {"name": "Candidate B"}]
        market["prices"] = {"Candidate A": 0.4, "Candidate B": 0.6}
        adapter.runtime.get_json = lambda url, **kwargs: market  # type: ignore[method-assign]

        contracts = adapter.list_contracts("space:btc-150k-2025")
        price = adapter.get_price("btc-150k-2025:Candidate A")

        self.assertEqual([contract.outcome for contract in contracts], ["Candidate A", "Candidate B"])
        self.assertEqual(contracts[0].contract_id, "btc-150k-2025:Candidate A")
        self.assertAlmostEqual(price.last or 0.0, 0.4)

    def test_path_order_and_feature_validation_are_clear(self) -> None:
        adapter, _ = self.make_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.get_price("../private:YES")
        with self.assertRaises(MarketConfigurationError):
            adapter.get_orderbook("btc-150k-2025:bad:outcome")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("space", "btc-150k-2025:YES", "HOLD", 1, 0.5))
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("btc-150k-2025:YES", limit=501)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("btc-150k-2025:YES", resolution="30m")

        order = PaperOrderRequest("space", "btc-150k-2025:YES", "BUY", 1, 0.5)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(order)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
