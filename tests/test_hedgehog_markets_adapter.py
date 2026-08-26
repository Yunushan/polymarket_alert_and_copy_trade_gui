from __future__ import annotations

import json
import base64
import unittest
from pathlib import Path

from market_adapters import HedgehogMarketsAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hedgehog_markets" / "program_accounts.json"
FIRST_MARKET = "GgBaCs3NCBuZN12kCJgAW63ydqohFkHEdfdEXBPzLHq"


class HedgehogMarketsAdapterTests(unittest.TestCase):
    def make_adapter(self, extra_config=None):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        config = {"hedgehog_rpc_url": "https://rpc.example.invalid"}
        config.update(extra_config or {})
        adapter = HedgehogMarketsAdapter(config)
        calls = []

        def fake_request(method, url, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://rpc.example.invalid")
            self.assertEqual(headers, {"Content-Type": "application/json"})
            rpc_method = json_body["method"]
            if rpc_method == "getProgramAccounts":
                return fixture
            if rpc_method == "getAccountInfo":
                for row in fixture["result"]:
                    if row["pubkey"] == json_body["params"][0]:
                        return {"jsonrpc": "2.0", "id": 1, "result": {"value": row["account"]}}
            if rpc_method == "sendTransaction":
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": "5Kq3f6nN8Y5zQqJwZc4hX2sY9mP7rT6vU8wA1bC2dE3F",
                }
            raise AssertionError(f"unexpected Hedgehog RPC method: {rpc_method}")

        adapter.runtime.request_json = fake_request  # type: ignore[method-assign]
        return adapter, calls

    def test_health_and_capabilities_are_truthful(self) -> None:
        adapter, _ = self.make_adapter()
        health = adapter.health_check()
        self.assertTrue(health["ok"])
        self.assertEqual(health["program_id"], "PARrVs6F5egaNuz8g6pKJyU4ze3eX5xGZCFb3GLiVvu")
        self.assertEqual(health["account_encoding"], "custom Borsh MarketV1")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)

    def test_program_accounts_contracts_prices_and_paper_intent(self) -> None:
        adapter, calls = self.make_adapter()
        events = adapter.list_events(limit=10)
        self.assertEqual([event.event_id for event in events], [FIRST_MARKET, "swqrv48gsrwpBFbftEwnP2vB4jckpvfGJfXkwaniLCC"])
        self.assertEqual(events[0].title, "Hedgehog market 7")
        self.assertEqual(events[0].status, "active")
        self.assertEqual(events[1].status, "resolved")

        contracts = adapter.list_contracts(FIRST_MARKET)
        self.assertEqual([contract.contract_id for contract in contracts], [f"{FIRST_MARKET}:0", f"{FIRST_MARKET}:1"])
        price = adapter.get_price(f"{FIRST_MARKET}:0")
        self.assertAlmostEqual(price.last or 0.0, 0.7)
        self.assertEqual(price.source, "hedgehog_parimutuel_onchain")

        paper = adapter.place_paper_order(
            PaperOrderRequest("hedgehog_markets", f"{FIRST_MARKET}:1", "BUY", 2.5, 0.35)
        )
        self.assertTrue(paper.accepted)
        self.assertAlmostEqual(paper.average_price or 0.0, 0.3)
        self.assertEqual(paper.raw["instruction"], "DepositV1")
        self.assertEqual(paper.raw["amount_raw"], 2_500_000)
        request = next(item for item in calls if item[2]["method"] == "getProgramAccounts")
        self.assertEqual(request[2]["params"][1]["filters"][0]["memcmp"]["bytes"], "4")

    def test_invalid_orders_and_unsupported_features_fail_closed(self) -> None:
        adapter, _ = self.make_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.get_price(f"{FIRST_MARKET}:9")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("hedgehog_markets", f"{FIRST_MARKET}:0", "SELL", 1))
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(PaperOrderRequest("hedgehog_markets", "../outside:0", "BUY", 1))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{FIRST_MARKET}:0")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(PaperOrderRequest("hedgehog_markets", f"{FIRST_MARKET}:0", "BUY", 1))
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})

    def test_signed_transaction_forwarding_is_fail_closed(self) -> None:
        adapter, calls = self.make_adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "hedgehog_submit_signed_transactions": True,
            }
        )
        amount_raw = 2_500_000
        instruction_data = bytes([4, 1]) + amount_raw.to_bytes(8, "little")
        signed = base64.b64encode(b"x" * 96).decode("ascii")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    "hedgehog_markets",
                    f"{FIRST_MARKET}:1",
                    "BUY",
                    2.5,
                    0.3,
                    {
                        "signed_transaction": signed,
                        "program_id": adapter.program_id,
                        "instruction": "DepositV1",
                        "market_account": FIRST_MARKET,
                        "option": 1,
                        "amount_raw": amount_raw,
                        "instruction_data": instruction_data.hex(),
                    },
                )
            )
        self.assertEqual(calls, [])

    def test_guarded_live_order_rejects_unreviewed_metadata(self) -> None:
        adapter, calls = self.make_adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "hedgehog_submit_signed_transactions": True,
            }
        )
        signed = base64.b64encode(b"x" * 96).decode("ascii")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    "hedgehog_markets",
                    f"{FIRST_MARKET}:0",
                    "BUY",
                    1,
                    metadata={
                        "signed_transaction": signed,
                        "program_id": adapter.program_id,
                        "instruction": "DepositV1",
                        "market_account": FIRST_MARKET,
                        "option": 0,
                        "amount_raw": 1_000_000,
                        "instruction_data": "04000000000000000000",
                    },
                )
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
