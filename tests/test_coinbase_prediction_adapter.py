from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import CoinbasePredictionMarketsAdapter, PaperOrderRequest
from market_adapters.errors import UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "coinbase_prediction_markets"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class CoinbasePredictionMarketsAdapterTests(unittest.TestCase):
    def make_adapter(self) -> CoinbasePredictionMarketsAdapter:
        adapter = CoinbasePredictionMarketsAdapter()
        markets = load_fixture("markets")
        orderbook = load_fixture("orderbook")
        trades = load_fixture("trades")
        candlesticks = load_fixture("candlesticks")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/markets"):
                event_ticker = (params or {}).get("event_ticker")
                if event_ticker:
                    filtered = [
                        market for market in markets["markets"] if market.get("event_ticker") == event_ticker
                    ]
                    return {"markets": filtered, "cursor": ""}
                return markets
            if url.endswith("/markets/KXFED-26MAY-TARGET-425"):
                return {"market": markets["markets"][0]}
            if url.endswith("/markets/KXFED-26MAY-TARGET-425/orderbook"):
                return orderbook
            if url.endswith("/markets/trades"):
                return trades
            if url.endswith("/series/KXFED/markets/KXFED-26MAY-TARGET-425/candlesticks"):
                return candlesticks
            raise AssertionError(f"unexpected Coinbase/Kalshi URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_alias_health_and_capabilities_are_explicit(self) -> None:
        adapter = CoinbasePredictionMarketsAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "coinbase_prediction_markets")
        self.assertEqual(health["alias_of"], "kalshi")
        self.assertEqual(health["provider"], "Coinbase Financial Markets / Kalshi")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertIn("external-api.kalshi.com", health["api_base_url"])

    def test_public_kalshi_venue_mapping_supports_events_contracts_prices_and_orderbooks(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("fed", limit=10)
        contracts = adapter.list_contracts("KXFED-26MAY")
        price = adapter.get_price("KXFED-26MAY-TARGET-425:YES")
        book = adapter.get_orderbook("KXFED-26MAY-TARGET-425:YES")
        trades = adapter.list_trades("KXFED-26MAY-TARGET-425:YES", limit=10)
        candles = adapter.list_candles(
            "KXFED-26MAY-TARGET-425:YES",
            from_timestamp=1777630000,
            to_timestamp=1777640000,
        )

        self.assertEqual(events[0].market_id, "coinbase_prediction_markets")
        self.assertEqual(len(contracts), 4)
        self.assertEqual(price.bid, 0.41)
        self.assertEqual(price.ask, 0.42)
        self.assertEqual([level.price for level in book.asks], [0.42, 0.44])
        self.assertEqual(trades[0].trade_id, "trade-kalshi-1")
        self.assertEqual(trades[0].market_id, "coinbase_prediction_markets")
        self.assertEqual(candles[0].close, 0.42)
        self.assertEqual(candles[0].market_id, "coinbase_prediction_markets")

    def test_paper_orders_are_local_and_coinbase_live_copy_paths_fail_closed(self) -> None:
        adapter = self.make_adapter()

        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="coinbase_prediction_markets",
                contract_id="KXFED-26MAY-TARGET-425:YES",
                side="BUY",
                size=2,
                limit_price=0.42,
            )
        )
        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)

        with self.assertRaises(UnsupportedFeatureError) as live_ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="coinbase_prediction_markets",
                    contract_id="KXFED-26MAY-TARGET-425:YES",
                    side="BUY",
                    size=1,
                    limit_price=0.42,
                )
            )
        self.assertEqual(live_ctx.exception.feature, "live_trading")

        with self.assertRaises(UnsupportedFeatureError) as copy_ctx:
            adapter.copy_trade_from_activity({})
        self.assertEqual(copy_ctx.exception.feature, "copy_trading")


if __name__ == "__main__":
    unittest.main()
