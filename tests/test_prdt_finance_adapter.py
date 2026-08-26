from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import PRDTFinanceAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "prdt_finance" / "rpc_responses.json"


class PRDTFinanceAdapterTests(unittest.TestCase):
    def make_adapter(self, *, now: int = 1_700_000_030):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        config = {
            "prdt_rpc_url": "https://rpc.example.invalid",
            "prdt_amount_scale": 1_000_000,
            "prdt_prediction_contracts": [
                {
                    "address": fixture["prediction_address"],
                    "title": "BTC above its reference price?",
                    "asset": "BTC",
                    "factory_address": fixture["factory_address"],
                    "factory_index": 3,
                }
            ],
        }
        adapter = PRDTFinanceAdapter(config, clock=lambda: now)
        calls = []
        selectors = {
            "76671808": "current_epoch",
            "7d1cd04f": "interval_seconds",
            "fa968eea": "min_bet_amount",
            "78691f16": "bet_token",
            "7755244e": "oracle",
            "8c65c81f": "round",
            "8bc33af3": "timestamps",
        }

        def fake_request(method, url, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://rpc.example.invalid")
            self.assertEqual(headers, {"Content-Type": "application/json"})
            self.assertEqual(json_body["method"], "eth_call")
            call = json_body["params"][0]
            self.assertEqual(call["to"].lower(), fixture["prediction_address"].lower())
            fixture_key = selectors[call["data"][2:10]]
            result = fixture["responses"][fixture_key]
            if isinstance(result, list):
                result = "0x" + "".join(result)
            return {"jsonrpc": "2.0", "id": 1, "result": result}

        adapter.runtime.request_json = fake_request  # type: ignore[method-assign]
        return adapter, fixture, calls

    def test_health_and_capabilities_are_truthful(self) -> None:
        adapter, fixture, _ = self.make_adapter()

        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(health["configured_prediction_contracts"], [fixture["prediction_address"]])
        self.assertFalse(health["dynamic_discovery"])
        self.assertTrue(health["configuration_required"])
        self.assertEqual(health["contract_model"], "Prediction / PredictionFactory")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)

    def test_configured_prediction_round_contracts_and_pool_prices(self) -> None:
        adapter, fixture, calls = self.make_adapter()

        events = adapter.list_events(limit=10)

        event_id = f"{fixture['prediction_address']}:{fixture['epoch']}"
        self.assertEqual([event.event_id for event in events], [event_id])
        self.assertEqual(events[0].status, "open")
        contracts = adapter.list_contracts(event_id)
        self.assertEqual([contract.outcome for contract in contracts], ["BULL", "BEAR"])
        self.assertAlmostEqual(adapter.get_price(f"{event_id}:BULL").last or 0.0, 0.6)
        self.assertAlmostEqual(adapter.get_price(f"{event_id}:BEAR").last or 0.0, 0.4)
        self.assertEqual(adapter.get_price(f"{event_id}:BULL").source, "prdt_prediction_pool_share")
        self.assertGreaterEqual(len(calls), 7)

    def test_lifecycle_uses_clock_and_paper_intents_are_open_only(self) -> None:
        open_adapter, fixture, _ = self.make_adapter(now=1_700_000_030)
        event_id = f"{fixture['prediction_address']}:{fixture['epoch']}"
        result = open_adapter.place_paper_order(
            PaperOrderRequest("prdt_finance", f"{event_id}:BULL", "BUY", 2.5)
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["amount_raw"], 2_500_000)
        self.assertEqual(result.raw["minimum_bet_amount_raw"], 1_000_000)
        self.assertTrue(result.raw["unsigned_factory_call"]["data"].startswith("0x31dfce9e"))
        with self.assertRaisesRegex(MarketConfigurationError, "below the contract minimum"):
            open_adapter.place_paper_order(
                PaperOrderRequest("prdt_finance", f"{event_id}:BULL", "BUY", 0.5)
            )

        locked_adapter, _, _ = self.make_adapter(now=1_700_000_090)
        self.assertEqual(locked_adapter.list_events()[0].status, "locked")
        with self.assertRaisesRegex(MarketConfigurationError, "paper intents are accepted only before"):
            locked_adapter.place_paper_order(
                PaperOrderRequest("prdt_finance", f"{event_id}:BULL", "BUY", 1)
            )

        awaiting_adapter, _, _ = self.make_adapter(now=1_700_000_200)
        self.assertEqual(awaiting_adapter.list_events()[0].status, "awaiting_oracle")
        with self.assertRaisesRegex(MarketConfigurationError, "awaiting_oracle"):
            awaiting_adapter.place_paper_order(
                PaperOrderRequest("prdt_finance", f"{event_id}:BEAR", "BUY", 1)
            )

    def test_configuration_and_unsupported_paths_fail_closed(self) -> None:
        with self.assertRaisesRegex(MarketConfigurationError, "does not guess current deployments"):
            PRDTFinanceAdapter({"prdt_rpc_url": "https://rpc.example.invalid"}).list_events()

        adapter, fixture, _ = self.make_adapter()
        event_id = f"{fixture['prediction_address']}:{fixture['epoch']}"
        with self.assertRaises(MarketConfigurationError):
            adapter.list_contracts("../outside")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest("prdt_finance", f"{event_id}:BULL", "SELL", 1)
            )
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{event_id}:BULL")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest("prdt_finance", f"{event_id}:BULL", "BUY", 1)
            )
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
