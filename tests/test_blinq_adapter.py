from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import BlinqAdapter, PaperOrderRequest, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blinq"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class BlinqAdapterTests(unittest.TestCase):
    def test_health_identifies_polymarket_alias_and_blocks_blinq_execution(self) -> None:
        health = BlinqAdapter().health_check()

        self.assertEqual(health["market_id"], "blinq")
        self.assertEqual(health["alias_of"], "polymarket")
        self.assertFalse(health["blinq_leverage_api_supported"])
        self.assertFalse(health["blinq_wallet_api_supported"])
        self.assertFalse(health["live_trading_supported"])
        self.assertFalse(health["copy_trading_supported"])
        self.assertTrue(health["trade_history_requires_l2_auth"])

    def test_lists_polymarket_contracts_for_blinq_market(self) -> None:
        adapter = BlinqAdapter()
        market = load_fixture("market.json")
        with patch("market_adapters.polymarket.gamma.get_event_by_slug", return_value=None), patch(
            "market_adapters.polymarket.gamma.get_market_by_slug", return_value=market
        ):
            contracts = adapter.list_contracts("blinq-market-slug")

        self.assertEqual([contract.contract_id for contract in contracts], ["blinq-token-yes", "blinq-token-no"])
        self.assertTrue(all(contract.market_id == "blinq" for contract in contracts))

    def test_maps_public_orderbook_price_and_paper_order(self) -> None:
        adapter = BlinqAdapter()
        book = load_fixture("orderbook.json")
        with patch("market_adapters.polymarket.clob_rest.get_book", return_value=book), patch(
            "market_adapters.polymarket.clob_rest.get_midpoint", return_value=0.62
        ), patch("market_adapters.polymarket.clob_rest.get_last_trade_price", return_value=0.61):
            orderbook = adapter.get_orderbook("blinq-token-yes")
            price = adapter.get_price("blinq-token-yes")

        self.assertEqual(orderbook.market_id, "blinq")
        self.assertEqual(price.market_id, "blinq")
        self.assertEqual(price.midpoint, 0.62)
        paper = adapter.place_paper_order(
            PaperOrderRequest("blinq", "blinq-token-yes", "BUY", 2.0, 0.62)
        )
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.filled_size, 0.0)

    def test_exposes_underlying_polymarket_public_history_contracts(self) -> None:
        adapter = BlinqAdapter()
        with patch(
            "market_adapters.polymarket.clob_auth.get_trades",
            return_value=load_fixture("clob_trades.json"),
        ), patch(
            "market_adapters.polymarket.clob_rest.get_price_history",
            return_value=load_fixture("price_history.json"),
        ), patch.object(adapter, "_l2_read_headers", return_value={"POLY_ADDRESS": "fixture"}):
            trades = adapter.list_trades("blinq-token-yes", limit=2)
            candles = adapter.list_candles(
                "blinq-token-yes",
                resolution="1h",
                from_timestamp=1760000000,
                to_timestamp=1760000300,
            )

        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertEqual([trade.trade_id for trade in trades], ["clob-trade-1", "clob-trade-2"])
        self.assertEqual([trade.market_id for trade in trades], ["blinq", "blinq"])
        self.assertEqual([candle.close for candle in candles], [0.45, 0.47])
        self.assertEqual([candle.market_id for candle in candles], ["blinq", "blinq"])

    def test_leverage_live_and_copy_operations_fail_closed(self) -> None:
        adapter = BlinqAdapter()
        order = PaperOrderRequest("blinq", "blinq-token-yes", "BUY", 1.0, 0.60)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(order)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({"asset": "blinq-token-yes", "side": "BUY", "size": "1"})
