from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import DriftBetAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError, MarketHTTPError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "drift_bet"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class DriftBetAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        settings = {"drift_bet_market_symbols": [{"symbol": "BTC-ELECTION-BET", "title": "BTC election"}]}
        settings.update(config or {})
        adapter = DriftBetAdapter(settings)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            self.assertEqual(headers, {})
            self.assertTrue(url.endswith("/market/BTC-ELECTION-BET/predictions"))
            return load_fixture("predictions")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_health_and_catalog_surfaces_are_explicit(self) -> None:
        adapter, _ = self.make_adapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "drift_bet")
        self.assertEqual(health["configured_market_symbols"], ["BTC-ELECTION-BET"])
        self.assertFalse(health["dynamic_discovery"])
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertEqual(health["history_retention_days"], 31)
        self.assertEqual(health["history_page_limit"], 20)
        self.assertTrue(health["candle_history_derived"])

    def test_events_contracts_price_and_paper_order_use_official_prediction_shape(self) -> None:
        adapter, calls = self.make_adapter()

        events = adapter.list_events("election")
        contracts = adapter.list_contracts(events[0].event_id)
        yes_price = adapter.get_price("BTC-ELECTION-BET:YES")
        no_price = adapter.get_price("BTC-ELECTION-BET:NO")
        paper = adapter.place_paper_order(
            PaperOrderRequest("drift_bet", "BTC-ELECTION-BET:YES", "BUY", 3, 0.62)
        )

        self.assertEqual(events[0].event_id, "drift:BTC-ELECTION-BET")
        self.assertEqual(events[0].status, "active")
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        self.assertAlmostEqual(yes_price.midpoint or 0.0, 0.62)
        self.assertAlmostEqual(no_price.midpoint or 0.0, 0.38)
        self.assertEqual(yes_price.source, "drift_data_api_predictions")
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.62)
        self.assertGreaterEqual(len(calls), 4)

    def test_public_prediction_fills_normalize_trades_and_derived_candles(self) -> None:
        adapter, _ = self.make_adapter()

        yes_trades = adapter.list_trades("BTC-ELECTION-BET:YES")
        no_trades = adapter.list_trades("BTC-ELECTION-BET:NO")
        recent = adapter.list_trades("BTC-ELECTION-BET:YES", limit=1, after=1786999000)
        candles = adapter.list_candles("BTC-ELECTION-BET:YES", resolution="1h")

        self.assertEqual([trade.trade_id for trade in yes_trades], ["drift-fill-101", "drift-fill-100"])
        self.assertEqual([trade.side for trade in yes_trades], ["BUY", "SELL"])
        self.assertEqual([trade.price for trade in yes_trades], [0.62, 0.58])
        self.assertEqual([trade.size for trade in yes_trades], [1.0, 2.0])
        self.assertEqual([trade.side for trade in no_trades], ["SELL", "BUY"])
        self.assertAlmostEqual(no_trades[0].price, 0.38)
        self.assertAlmostEqual(no_trades[1].price, 0.42)
        self.assertEqual([trade.trade_id for trade in recent], ["drift-fill-101"])
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp, 1786996800.0)
        self.assertEqual(candles[0].open, 0.58)
        self.assertEqual(candles[0].high, 0.62)
        self.assertEqual(candles[0].low, 0.58)
        self.assertEqual(candles[0].close, 0.62)
        self.assertEqual(candles[0].volume, 3.0)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(candles[0].raw["retention_days"], 31)
        self.assertEqual(candles[0].raw["trade_ids"], ["drift-fill-100", "drift-fill-101"])

    def test_realistic_perp_fill_accepts_small_raw_units_and_skips_non_fills(self) -> None:
        adapter, _ = self.make_adapter()
        payload = {
            "success": True,
            "records": [
                {
                    "ts": 1787000000,
                    "txSig": "small-fill",
                    "txSigIndex": 0,
                    "slot": 300000000,
                    "quoteAssetAmountFilled": "310",
                    "baseAssetAmountFilled": "500000",
                    "marketType": "perp",
                    "symbol": "BTC-ELECTION-BET",
                    "action": "fill",
                    "fillRecordId": "drift-small-fill",
                    "takerOrderDirection": "long",
                },
                {
                    "ts": 1786999999,
                    "txSig": "cancel-record",
                    "txSigIndex": 0,
                    "slot": 299999999,
                    "quoteAssetAmountFilled": "310",
                    "baseAssetAmountFilled": "500000",
                    "marketType": "perp",
                    "symbol": "BTC-ELECTION-BET",
                    "action": "cancel",
                    "fillRecordId": "drift-not-a-fill",
                    "takerOrderDirection": "long",
                },
            ],
        }
        adapter.runtime.get_json = lambda *args, **kwargs: payload  # type: ignore[method-assign]

        trades = adapter.list_trades("BTC-ELECTION-BET:YES")

        self.assertEqual([trade.trade_id for trade in trades], ["drift-small-fill"])
        self.assertAlmostEqual(trades[0].price, 0.62)
        self.assertAlmostEqual(trades[0].size, 0.0005)

    def test_equal_timestamp_candles_use_slot_and_event_order(self) -> None:
        adapter, _ = self.make_adapter()
        payload = {
            "success": True,
            "records": [
                {
                    "ts": 1787000000,
                    "txSig": "same-slot",
                    "txSigIndex": 2,
                    "slot": 300000001,
                    "quoteAssetAmountFilled": "700000",
                    "baseAssetAmountFilled": "1000000000",
                    "marketType": "perp",
                    "symbol": "BTC-ELECTION-BET",
                    "action": "fill",
                    "fillRecordId": "newest",
                    "takerOrderDirection": "long",
                },
                {
                    "ts": 1787000000,
                    "txSig": "same-slot",
                    "txSigIndex": 1,
                    "slot": 300000001,
                    "quoteAssetAmountFilled": "650000",
                    "baseAssetAmountFilled": "1000000000",
                    "marketType": "perp",
                    "symbol": "BTC-ELECTION-BET",
                    "action": "fill",
                    "fillRecordId": "middle",
                    "takerOrderDirection": "long",
                },
                {
                    "ts": 1787000000,
                    "txSig": "older-slot",
                    "txSigIndex": 9,
                    "slot": 300000000,
                    "quoteAssetAmountFilled": "600000",
                    "baseAssetAmountFilled": "1000000000",
                    "marketType": "perp",
                    "symbol": "BTC-ELECTION-BET",
                    "action": "fill",
                    "fillRecordId": "oldest",
                    "takerOrderDirection": "long",
                },
            ],
        }
        adapter.runtime.get_json = lambda *args, **kwargs: payload  # type: ignore[method-assign]

        candle = adapter.list_candles("BTC-ELECTION-BET:YES", resolution="1h")[0]

        self.assertAlmostEqual(candle.open, 0.60)
        self.assertAlmostEqual(candle.close, 0.70)
        self.assertEqual(candle.raw["trade_ids"], ["oldest", "middle", "newest"])

    def test_trade_and_candle_bounds_fail_closed(self) -> None:
        adapter, _ = self.make_adapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("BTC-ELECTION-BET:YES", limit="many")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("BTC-ELECTION-BET:YES", before=10, after=20)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("BTC-ELECTION-BET:YES", resolution="2h")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("BTC-ELECTION-BET:YES", from_timestamp=20, to_timestamp=10)

    def test_missing_inventory_and_unsupported_features_fail_clearly(self) -> None:
        adapter = DriftBetAdapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.list_events()

        adapter, _ = self.make_adapter()
        order = PaperOrderRequest("drift_bet", "BTC-ELECTION-BET:YES", "BUY", 1, 0.5)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(order.contract_id)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(order)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})

        adapter.runtime.get_json = lambda *args, **kwargs: {"success": True, "records": []}  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.get_price(order.contract_id)

    def test_symbol_and_order_validation_blocks_path_injection(self) -> None:
        with self.assertRaises(MarketConfigurationError):
            DriftBetAdapter({"drift_bet_market_symbols": ["../private"]}).health_check()

        adapter, _ = self.make_adapter()
        for contract_id in ("../private:YES", "BTC-ELECTION-BET:maybe"):
            with self.subTest(contract_id=contract_id):
                with self.assertRaises(MarketConfigurationError):
                    adapter.get_price(contract_id)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("drift_bet", "BTC-ELECTION-BET:YES", "HOLD", 1, 0.5))


if __name__ == "__main__":
    unittest.main()
