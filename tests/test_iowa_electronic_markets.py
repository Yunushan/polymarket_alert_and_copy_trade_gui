from __future__ import annotations

import unittest
from pathlib import Path
from market_adapters import IowaElectronicMarketsAdapter, PaperOrderRequest
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "iowa_electronic_markets" / "powell_price_data.txt"


class _Response:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text


class IowaElectronicMarketsAdapterTests(unittest.TestCase):
    def test_archive_events_contracts_candles_latest_price_and_paper_order(self) -> None:
        payload = FIXTURE.read_text(encoding="utf-8")
        adapter = IowaElectronicMarketsAdapter()
        calls = []

        def fake_request(method: str, url: str, *, headers=None, timeout=None, **kwargs):
            calls.append((method, url, headers, timeout, kwargs))
            return _Response(payload)

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        events = adapter.list_events("powell")
        contracts = adapter.list_contracts(events[0].event_id)
        candles = adapter.list_candles("1996-powell-nomination:P.YES")
        price = adapter.get_price("1996-powell-nomination:P.YES")
        paper = adapter.place_paper_order(
            PaperOrderRequest(
                "iowa_electronic_markets",
                "1996-powell-nomination:P.YES",
                "BUY",
                2,
            )
        )

        self.assertEqual(events[0].event_id, "archive:1996-powell-nomination")
        self.assertEqual([contract.contract_id for contract in contracts], [
            "1996-powell-nomination:P.YES",
            "1996-powell-nomination:P.NO",
        ])
        self.assertEqual(len(candles), 2)
        self.assertEqual([candle.close for candle in candles], [0.2, 0.2])
        self.assertEqual([candle.open for candle in candles], [0.2, 0.2])
        self.assertEqual([candle.volume for candle in candles], [1.675, 0.0])
        self.assertEqual(price.last, 0.2)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.2)
        self.assertTrue(all(call[0] == "GET" for call in calls))
        self.assertTrue(all(call[1].endswith("powellpricedata.txt") for call in calls))

    def test_archive_filters_time_and_rejects_unsupported_or_unsafe_paths(self) -> None:
        adapter = IowaElectronicMarketsAdapter()
        adapter.runtime.session.request = lambda *args, **kwargs: _Response(FIXTURE.read_text(encoding="utf-8"))  # type: ignore[method-assign]
        all_candles = adapter.list_candles("1996-powell-nomination:P.YES")
        filtered = adapter.list_candles(
            "1996-powell-nomination:P.YES",
            from_timestamp=all_candles[-1].timestamp,
            to_timestamp=all_candles[-1].timestamp,
        )
        self.assertEqual(len(filtered), 1)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("1996-powell-nomination:P.YES", resolution="1h")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                "1996-powell-nomination:P.YES",
                from_timestamp=all_candles[-1].timestamp,
                to_timestamp=all_candles[0].timestamp - 1,
            )
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook("1996-powell-nomination:P.YES")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    "iowa_electronic_markets",
                    "1996-powell-nomination:P.YES",
                    "BUY",
                    1,
                )
            )
        with self.assertRaises(MarketConfigurationError):
            IowaElectronicMarketsAdapter({
                "iem_historical_markets": [{
                    "market_id": "unsafe",
                    "data_url": "https://evil.example/archive.txt",
                    "contracts": {"P.YES": "Yes"},
                }]
            }).list_candles("unsafe:P.YES")


if __name__ == "__main__":
    unittest.main()
