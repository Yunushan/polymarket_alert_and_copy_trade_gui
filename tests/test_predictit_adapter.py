from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import PaperOrderRequest, PredictItAdapter
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "predictit"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class PredictItAdapterTests(unittest.TestCase):
    def make_adapter(self) -> PredictItAdapter:
        adapter = PredictItAdapter()
        all_markets = load_fixture("all")
        market = load_fixture("market")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/all"):
                return all_markets
            if url.endswith("/markets/7053"):
                return market
            raise AssertionError(f"unexpected PredictIt URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_registered_metadata_advertises_supported_predictit_features(self) -> None:
        adapter = PredictItAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "predictit")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertIn("predictit.org/api/marketdata", health["api_base_url"])
        self.assertTrue(health["orderbook_supported"])
        self.assertFalse(health["orderbook_depth_supported"])

    def test_list_events_uses_public_marketdata_feed_and_filters_query(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("gop", limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_id, "predictit")
        self.assertEqual(events[0].event_id, "7053")
        self.assertEqual(events[0].status, "active")
        self.assertIn("Republican", events[0].title)

    def test_list_contracts_creates_yes_and_no_contracts_for_each_contract(self) -> None:
        adapter = self.make_adapter()

        contracts = adapter.list_contracts("7053")

        self.assertEqual(len(contracts), 4)
        self.assertEqual(contracts[0].contract_id, "7053:24680:YES")
        self.assertEqual(contracts[1].contract_id, "7053:24680:NO")
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertEqual(contracts[1].outcome, "No")

    def test_get_price_maps_yes_and_no_top_of_book_prices(self) -> None:
        adapter = self.make_adapter()

        yes = adapter.get_price("7053:24680:YES")
        no = adapter.get_price("7053:24680:NO")

        self.assertEqual(yes.bid, 0.41)
        self.assertEqual(yes.ask, 0.43)
        self.assertEqual(yes.last, 0.42)
        self.assertAlmostEqual(yes.midpoint or 0, 0.42)
        self.assertEqual(no.bid, 0.57)
        self.assertEqual(no.ask, 0.59)
        self.assertAlmostEqual(no.last or 0, 0.58)
        self.assertAlmostEqual(no.midpoint or 0, 0.58)

    def test_orderbook_exposes_documented_top_of_book_quotes_without_depth(self) -> None:
        adapter = self.make_adapter()

        yes = adapter.get_orderbook("7053:24680:YES")
        no = adapter.get_orderbook("7053:24680:NO")

        self.assertEqual([(level.price, level.size) for level in yes.bids], [(0.41, 0.0)])
        self.assertEqual([(level.price, level.size) for level in yes.asks], [(0.43, 0.0)])
        self.assertEqual([(level.price, level.size) for level in no.bids], [(0.57, 0.0)])
        self.assertEqual([(level.price, level.size) for level in no.asks], [(0.59, 0.0)])
        self.assertEqual(yes.raw["depth"], "top_of_book_only")
        self.assertFalse(yes.raw["size_available"])

    def test_live_and_copy_trading_are_unsupported(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(UnsupportedFeatureError) as live_ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="predictit",
                    contract_id="7053:24680:YES",
                    side="BUY",
                    size=1,
                    limit_price=0.42,
                )
            )
        self.assertEqual(live_ctx.exception.feature, "live_trading")
        self.assertIn("does not publish an official automated trading API", str(live_ctx.exception))

        with self.assertRaises(UnsupportedFeatureError) as copy_ctx:
            adapter.copy_trade_from_activity({})
        self.assertEqual(copy_ctx.exception.feature, "copy_trading")

    def test_paper_order_is_dry_run_and_validates_input(self) -> None:
        adapter = self.make_adapter()

        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="predictit",
                contract_id="7053:24680:NO",
                side="SELL",
                size=12,
                limit_price=0.58,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertEqual(result.contract_id, "7053:24680:NO")
        self.assertEqual(result.raw["outcome"], "NO")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="predictit",
                    contract_id="7053:24680:MAYBE",
                    side="BUY",
                    size=1,
                    limit_price=0.42,
                )
            )


if __name__ == "__main__":
    unittest.main()
