from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import PaperOrderRequest, SciCastAdapter
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError


ROOT = Path(__file__).resolve().parent / "fixtures" / "scicast"


class SciCastAdapterTests(unittest.TestCase):
    def make_adapter(self) -> SciCastAdapter:
        adapter = SciCastAdapter({"scicast_api_base_url": "https://fixture.scicast.test", "scicast_allow_custom_base_url": True})
        questions = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
        history = json.loads((ROOT / "question_history.json").read_text(encoding="utf-8"))
        trades = json.loads((ROOT / "trade_history.json").read_text(encoding="utf-8"))

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/question/"):
                return questions
            if url.endswith("/question_history/"):
                question_id = str((params or {}).get("question_id") or "")
                return {"history": [row for row in history["history"] if row.get("question_id") == question_id]}
            if url.endswith("/trade_history/"):
                return trades
            raise AssertionError(f"unexpected SciCast URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_archive_discovery_contracts_prices_history_trades_and_paper(self) -> None:
        adapter = self.make_adapter()
        with patch.dict(os.environ, {"SCICAST_API_KEY": "fixture-key"}):
            events = adapter.list_events("summit")
            contracts = adapter.list_contracts(events[0].event_id)
            yes = adapter.get_price("q1001:YES")
            no = adapter.get_price("q1001:NO")
            candles = adapter.list_candles("q1001:YES", resolution="forecast")
            trades = adapter.list_trades("q1001:YES")
            paper = adapter.place_paper_order(
                PaperOrderRequest("scicast", "q1001:YES", "BUY", 2)
            )

        self.assertEqual(events[0].event_id, "question:q1001")
        self.assertEqual([contract.contract_id for contract in contracts], ["q1001:YES", "q1001:NO"])
        self.assertEqual(yes.last, 0.72)
        self.assertAlmostEqual(no.last or 0, 0.28)
        self.assertEqual([candle.close for candle in candles], [0.40, 0.72])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "BUY")
        self.assertEqual(trades[0].size, 12.5)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.72)

    def test_multiple_choice_and_archive_safety_boundaries(self) -> None:
        adapter = self.make_adapter()
        with patch.dict(os.environ, {"SCICAST_API_KEY": "fixture-key"}):
            contracts = adapter.list_contracts("question:q1002")
            choice = adapter.get_price("q1002:CHOICE:1")
        self.assertEqual([contract.contract_id for contract in contracts], ["q1002:CHOICE:0", "q1002:CHOICE:1", "q1002:CHOICE:2"])
        self.assertEqual(choice.last, 0.50)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook("q1001:YES")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("scicast", "q1001:YES", "BUY", 1))
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("q1001:YES", resolution="5m")

    def test_credential_and_host_guards(self) -> None:
        adapter = SciCastAdapter()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_events()
        with self.assertRaises(MarketConfigurationError):
            SciCastAdapter({"scicast_api_base_url": "https://evil.example"}).health_check()


if __name__ == "__main__":
    unittest.main()
