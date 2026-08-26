from __future__ import annotations

import unittest
from pathlib import Path

from market_adapters import HypermindAdapter, PaperOrderRequest
from market_adapters.errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "hypermind"
PRICES_URL = "https://fixture.example/hypermind/prices.csv"
OUTCOMES_URL = "https://fixture.example/hypermind/outcomes.txt"


class _Response:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text


class HypermindAdapterTests(unittest.TestCase):
    def _adapter(self) -> HypermindAdapter:
        adapter = HypermindAdapter(
            {
                "hypermind_prices_url": PRICES_URL,
                "hypermind_outcomes_url": OUTCOMES_URL,
                "hypermind_allow_custom_data_host": True,
            }
        )
        prices = (FIXTURE_ROOT / "prices.csv").read_text(encoding="utf-8")
        outcomes = (FIXTURE_ROOT / "outcomes.txt").read_text(encoding="utf-8")

        def fake_request(method: str, url: str, *, headers=None, timeout=None, **kwargs):
            del method, headers, timeout, kwargs
            return _Response(prices if url == PRICES_URL else outcomes)

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        return adapter

    def test_archive_exports_cover_events_contracts_prices_history_and_paper_orders(self) -> None:
        adapter = self._adapter()
        events = adapter.list_events("MKT1")
        contracts = adapter.list_contracts(events[0].event_id)
        price = adapter.get_price("MKT1:yes")
        trades = adapter.list_trades("MKT1:yes")
        candles = adapter.list_candles("MKT1:yes")
        paper = adapter.place_paper_order(PaperOrderRequest("hypermind", "MKT1:yes", "BUY", 2))

        self.assertEqual(events[0].event_id, "archive:MKT1")
        self.assertEqual([contract.contract_id for contract in contracts], ["MKT1:no", "MKT1:yes"])
        self.assertEqual(price.last, 0.4)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].side, "TRADE")
        self.assertEqual(trades[0].size, 5.0)
        self.assertEqual([candle.close for candle in candles], [0.25, 0.4])
        self.assertTrue(all(candle.raw["derived"] for candle in candles))
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.4)
        self.assertTrue(adapter.health_check()["archive_only"])

    def test_history_filters_and_fail_closed_boundaries(self) -> None:
        adapter = self._adapter()
        candles = adapter.list_candles("MKT1:yes")
        filtered = adapter.list_candles(
            "MKT1:yes",
            from_timestamp=candles[-1].timestamp,
            to_timestamp=candles[-1].timestamp,
        )
        self.assertEqual(len(filtered), 1)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("MKT1:yes", resolution="5m")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("MKT1:yes", after=candles[-1].timestamp, before=candles[0].timestamp)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook("MKT1:yes")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("hypermind", "MKT1:yes", "BUY", 1))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({"market_id": "hypermind"})

    def test_export_validation_and_custom_host_gate(self) -> None:
        with self.assertRaises(MarketConfigurationError):
            HypermindAdapter({"hypermind_prices_url": "http://predict.hypermind.com/prices.csv"}).health_check()
        with self.assertRaises(MarketConfigurationError):
            HypermindAdapter({"hypermind_prices_url": PRICES_URL}).health_check()

        adapter = self._adapter()
        adapter.runtime.session.request = lambda *args, **kwargs: _Response(
            "timestamp,wrong,outcome,price,qty\n2026-01-01,MKT1,yes,50,1\n"
        )  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.list_candles("MKT1:yes")


if __name__ == "__main__":
    unittest.main()
