from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import FrenzyFinanceAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frenzy_finance" / "rpc_responses.json"
MARKET_ID = "0x" + "22" * 32
EVENT_ID = f"frenzy:{MARKET_ID}:1700002226"


class FrenzyFinanceAdapterTests(unittest.TestCase):
    def make_adapter(self, *, with_spec: bool = True):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        config = {
            "frenzy_rpc_url": "https://rpc.example.invalid",
            "frenzy_from_block": "0x100",
            "frenzy_to_block": "0x200",
        }
        if with_spec:
            config["frenzy_market_specs"] = [
                {
                    "market_id": MARKET_ID,
                    "title": "Will BTC remain inside the next grid bucket?",
                    "interval_start": 1700002196,
                    "interval_end": 1700002226,
                    "status": "active",
                    "contracts": [
                        {"price_low": 100, "price_high": 101, "price": 0.4},
                        {"price_low": 101, "price_high": 102, "multiplier": 3},
                    ],
                }
            ]
        adapter = FrenzyFinanceAdapter(config)
        calls = []

        def fake_request(method, url, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://rpc.example.invalid")
            self.assertEqual(headers, {"Content-Type": "application/json"})
            rpc_method = json_body["method"]
            if rpc_method == "eth_blockNumber":
                return fixture["block_number"]
            if rpc_method == "eth_getLogs":
                return fixture["settled_logs"]
            raise AssertionError(f"unexpected Frenzy RPC method: {rpc_method}")

        adapter.runtime.request_json = fake_request  # type: ignore[method-assign]
        return adapter, calls

    def test_health_and_contract_capabilities_are_truthful(self) -> None:
        adapter, _ = self.make_adapter()
        health = adapter.health_check()
        self.assertTrue(health["ok"])
        self.assertEqual(health["chain_id"], 8453)
        self.assertEqual(health["contract_model"], "BetIntent / BetSettled")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)

    def test_configured_grid_and_settlement_history(self) -> None:
        adapter, calls = self.make_adapter()
        events = adapter.list_events(limit=10)
        self.assertEqual([event.event_id for event in events], [EVENT_ID])
        self.assertEqual(events[0].status, "active")
        self.assertIn("settlements", events[0].raw)

        contracts = adapter.list_contracts(EVENT_ID)
        self.assertEqual([contract.contract_id for contract in contracts], [f"{EVENT_ID}:0", f"{EVENT_ID}:1"])
        self.assertEqual(contracts[0].outcome, "100-101")
        self.assertAlmostEqual(adapter.get_price(f"{EVENT_ID}:0").last or 0.0, 0.4)
        self.assertAlmostEqual(adapter.get_price(f"{EVENT_ID}:1").last or 0.0, 1 / 3)

        log_request = next(item for item in calls if item[2]["method"] == "eth_getLogs")
        self.assertEqual(log_request[2]["params"][0]["address"], adapter.contract_address)
        self.assertEqual(log_request[2]["params"][0]["fromBlock"], "0x100")
        self.assertEqual(log_request[2]["params"][0]["toBlock"], "0x200")

    def test_paper_order_emits_eip712_intent_without_signing(self) -> None:
        adapter, _ = self.make_adapter()
        order = PaperOrderRequest(
            "frenzy_finance",
            f"{EVENT_ID}:1",
            "BUY",
            2.5,
            metadata={
                "bettor": "0x" + "b" * 40,
                "nonce": 7,
                "deadline": 1700002500,
            },
        )
        result = adapter.place_paper_order(order)
        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["amount_raw"], 2_500_000)
        self.assertTrue(result.raw["oracle_ack_required"])
        self.assertEqual(result.raw["eip712"]["message"]["marketId"], MARKET_ID)
        self.assertEqual(result.raw["eip712"]["message"]["priceLow"], 101)
        self.assertEqual(result.raw["eip712"]["message"]["priceHigh"], 102)
        self.assertEqual(result.raw["eip712"]["message"]["nonce"], 7)

    def test_settlement_only_events_remain_read_only(self) -> None:
        adapter, _ = self.make_adapter(with_spec=False)
        events = adapter.list_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "settled")
        contracts = adapter.list_contracts(events[0].event_id)
        self.assertEqual(contracts[0].outcome, "SETTLED")
        price = adapter.get_price(contracts[0].contract_id)
        self.assertAlmostEqual(price.last or 0.0, 1.0)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("frenzy_finance", contracts[0].contract_id, "BUY", 1))

    def test_invalid_and_unsupported_paths_fail_closed(self) -> None:
        adapter, _ = self.make_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.list_contracts("frenzy:../outside:1700002226")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("frenzy_finance", f"{EVENT_ID}:0", "SELL", 1))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{EVENT_ID}:0")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("frenzy_finance", f"{EVENT_ID}:0", "BUY", 1))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})


if __name__ == "__main__":
    unittest.main()
