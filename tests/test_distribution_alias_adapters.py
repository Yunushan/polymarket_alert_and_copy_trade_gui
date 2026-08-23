from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import (
    DraftKingsPredictionsAdapter,
    KalshiViaRobinhoodAdapter,
    PaperOrderRequest,
    RobinhoodPredictionMarketsAdapter,
    UnsupportedFeatureError,
)


ROOT = Path(__file__).resolve().parent / "fixtures"
ROBINHOOD_FIXTURES = ROOT / "robinhood_prediction_markets"
DRAFTKINGS_FIXTURES = ROOT / "draftkings_predictions"
KALSHI_CONTRACT = "KXFED-26MAY-TARGET-425:YES"
DRAFTKINGS_EVENT = "draftkings-cdna-election-2026"
DRAFTKINGS_CONTRACT = "DK-CDNA-ELECTION-YES"


def load_fixture(directory: Path, name: str):
    return json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))


class RobinhoodDistributionAliasTests(unittest.TestCase):
    def make_adapter(self, adapter_cls):
        adapter = adapter_cls({"min_request_interval_seconds": 0})
        markets = load_fixture(ROBINHOOD_FIXTURES, "markets")
        orderbook = load_fixture(ROBINHOOD_FIXTURES, "orderbook")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/markets"):
                return markets
            if url.endswith("/markets/KXFED-26MAY-TARGET-425"):
                return {"market": markets["markets"][0]}
            if url.endswith("/markets/KXFED-26MAY-TARGET-425/orderbook"):
                return orderbook
            if url.endswith("/markets/trades"):
                return load_fixture(ROBINHOOD_FIXTURES, "trades")
            if url.endswith("/series/KXFED/markets/KXFED-26MAY-TARGET-425/candlesticks"):
                return load_fixture(ROBINHOOD_FIXTURES, "candlesticks")
            raise AssertionError(f"unexpected Robinhood/Kalshi URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_both_aliases_expose_public_kalshi_data_and_history(self) -> None:
        for adapter_cls in (RobinhoodPredictionMarketsAdapter, KalshiViaRobinhoodAdapter):
            with self.subTest(adapter_cls=adapter_cls):
                adapter = self.make_adapter(adapter_cls)
                health = adapter.health_check()

                self.assertTrue(health["ok"])
                self.assertEqual(health["alias_of"], "kalshi")
                self.assertEqual(health["provider"], "Robinhood Derivatives / KalshiEX")
                self.assertTrue(adapter.capabilities.trade_history)
                self.assertTrue(adapter.capabilities.candle_history)
                self.assertFalse(adapter.capabilities.live_trading)
                self.assertFalse(adapter.capabilities.copy_trading)
                self.assertEqual(health["order_management_operations"], [])

                events = adapter.list_events("fed")
                contracts = adapter.list_contracts("KXFED-26MAY")
                price = adapter.get_price(KALSHI_CONTRACT)
                book = adapter.get_orderbook(KALSHI_CONTRACT)
                trades = adapter.list_trades(KALSHI_CONTRACT, limit=10)
                candles = adapter.list_candles(
                    KALSHI_CONTRACT,
                    from_timestamp=1777630000,
                    to_timestamp=1777640000,
                )

                self.assertEqual(events[0].market_id, adapter.market_id)
                self.assertEqual(len(contracts), 2)
                self.assertEqual(price.bid, 0.41)
                self.assertEqual(book.asks[0].price, 0.42)
                self.assertEqual(trades[0].trade_id, "trade-robinhood-1")
                self.assertEqual(candles[0].close, 0.42)

                paper = adapter.place_paper_order(
                    PaperOrderRequest(adapter.market_id, KALSHI_CONTRACT, "BUY", 1, 0.42)
                )
                self.assertTrue(paper.accepted)
                with self.assertRaises(UnsupportedFeatureError):
                    adapter.place_live_order(PaperOrderRequest(adapter.market_id, KALSHI_CONTRACT, "BUY", 1, 0.42))
                with self.assertRaises(UnsupportedFeatureError):
                    adapter.copy_trade_from_activity({})


class DraftKingsPredictionsAliasTests(unittest.TestCase):
    def make_adapter(self, config=None):
        adapter = DraftKingsPredictionsAdapter(config or {"min_request_interval_seconds": 0})
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/events/search") or url.endswith("/events"):
                return load_fixture(DRAFTKINGS_FIXTURES, "events")
            if url.endswith(f"/events/{DRAFTKINGS_EVENT}/contracts"):
                return load_fixture(DRAFTKINGS_FIXTURES, "contracts")
            if url.endswith(f"/contracts/{DRAFTKINGS_CONTRACT}/price"):
                return load_fixture(DRAFTKINGS_FIXTURES, "price")
            raise AssertionError(f"unexpected DraftKings/CDNA URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_alias_scope_and_public_cdna_payload_mapping(self) -> None:
        adapter, calls = self.make_adapter({"draftkings_predictions_api_base_url": "https://fixture.invalid/api"})
        health = adapter.health_check()

        self.assertEqual(health["alias_of"], "crypto_com_predict")
        self.assertEqual(health["underlying_market_data_provider"], "Crypto.com Derivatives North America (CDNA)")
        self.assertFalse(health["draftkings_order_api_supported"])
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)

        events = adapter.list_events("election", limit=5)
        contracts = adapter.list_contracts(DRAFTKINGS_EVENT)
        price = adapter.get_price(DRAFTKINGS_CONTRACT)

        self.assertEqual(events[0].market_id, "draftkings_predictions")
        self.assertEqual(contracts[0].contract_id, DRAFTKINGS_CONTRACT)
        self.assertEqual(price.midpoint, 0.5)
        self.assertEqual(price.source, "cdna_prediction_markets_market_data")
        self.assertTrue(all(url.startswith("https://fixture.invalid/api/") for url, _, _ in calls))

    def test_optional_key_paper_orders_and_unsupported_execution(self) -> None:
        adapter, calls = self.make_adapter({"draftkings_predictions_api_key": "fixture-key"})
        adapter.list_events(limit=1)
        self.assertEqual(calls[-1][2], {"X-API-Key": "fixture-key"})
        self.assertEqual(adapter.health_check()["api_key_source"], "config:draftkings_predictions_api_key")

        paper = adapter.place_paper_order(
            PaperOrderRequest("draftkings_predictions", DRAFTKINGS_CONTRACT, "BUY", 2, 0.5)
        )
        self.assertTrue(paper.accepted)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(DRAFTKINGS_CONTRACT)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("draftkings_predictions", DRAFTKINGS_CONTRACT, "BUY", 1, 0.5))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
