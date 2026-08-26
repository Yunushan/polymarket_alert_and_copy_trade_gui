from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path

from market_adapters import LamasFinanceAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError, MarketHTTPError
from market_adapters.lamas_finance import _base58_decode


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lamas_finance" / "rpc_responses.json"
SIGNATURE = "5Kq3f6nN8Y5zQqJwZc4hX2sY9mP7rT6vU8wA1bC2dE3F"


class LamasFinanceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.clock = lambda: 1_700_000_100
        self.adapter = self.make_adapter()
        self.calls = []

    def make_adapter(self, config=None):
        adapter = LamasFinanceAdapter(
            {"lamas_finance_rpc_url": "https://rpc.example.invalid", **(config or {})},
            clock=self.clock,
        )
        adapter.runtime.request_json = self._rpc  # type: ignore[method-assign]
        return adapter

    def _rpc(self, method: str, url: str, *, params=None, json_body=None, headers=None):
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://rpc.example.invalid")
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertIsNotNone(json_body)
        self.calls.append(json_body)
        rpc_method = json_body["method"]
        if rpc_method == "getProgramAccounts":
            program_id = json_body["params"][0]
            if program_id == self.adapter.up_or_down_program_id:
                return {"jsonrpc": "2.0", "id": 1, "result": self.fixture["program_accounts"]["up_or_down"]}
            if program_id == self.adapter.price_program_id:
                return {"jsonrpc": "2.0", "id": 1, "result": self.fixture["program_accounts"]["price_predict"]}
            raise AssertionError(f"unexpected Lamas program: {program_id}")
        if rpc_method == "getAccountInfo":
            pubkey = json_body["params"][0]
            for game, rows in self.fixture["program_accounts"].items():
                if rows[0]["pubkey"] == pubkey:
                    return {"jsonrpc": "2.0", "id": 1, "result": self.fixture["account_info"][game]}
            raise AssertionError(f"unexpected Lamas account: {pubkey}")
        if rpc_method == "sendTransaction":
            return {"jsonrpc": "2.0", "id": 1, "result": SIGNATURE}
        raise AssertionError(f"unexpected Lamas RPC method: {rpc_method}")

    @property
    def up_down_event(self) -> str:
        return f"up_or_down:{self.fixture['program_accounts']['up_or_down'][0]['pubkey']}"

    @property
    def price_event(self) -> str:
        return f"price_predict:{self.fixture['program_accounts']['price_predict'][0]['pubkey']}"

    def test_health_events_contracts_and_prices(self) -> None:
        health = self.adapter.health_check()
        self.assertTrue(health["ok"])
        self.assertEqual(health["cluster"], "devnet")
        self.assertEqual(health["price_predict_program_id"], self.adapter.price_program_id)
        self.assertTrue(health["wallet_transaction_required"])
        self.assertFalse(health["copy_trading_supported"])

        events = self.adapter.list_events(limit=10)
        self.assertEqual([event.event_id for event in events], [self.up_down_event, self.price_event])
        self.assertEqual([event.status for event in events], ["active", "active"])

        contracts = self.adapter.list_contracts(self.up_down_event)
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        self.assertAlmostEqual(self.adapter.get_price(contracts[0].contract_id).last or 0.0, 0.6)
        self.assertAlmostEqual(self.adapter.get_price(contracts[1].contract_id).last or 0.0, 0.4)

        price_contract = self.adapter.list_contracts(self.price_event)[0]
        self.assertEqual(price_contract.outcome, "REFERENCE")
        self.assertAlmostEqual(self.adapter.get_price(price_contract.contract_id).last or 0.0, 21.0)

    def test_paper_orders_build_exact_anchor_predict_intents(self) -> None:
        yes_id = f"{self.up_down_event}:YES"
        result = self.adapter.place_paper_order(PaperOrderRequest("lamas_finance", yes_id, "BUY", 0.000001, 0.6))
        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["amount_raw"], 1_000)
        self.assertEqual(result.raw["instruction"], "predict")
        self.assertEqual(len(bytes.fromhex(result.raw["instruction_data"])), 17)
        self.assertEqual(result.raw["is_up"], True)

        price_id = f"{self.price_event}:REFERENCE"
        price_result = self.adapter.place_paper_order(
            PaperOrderRequest("lamas_finance", price_id, "BUY", 0.000001, metadata={"predict_price": 42.5})
        )
        self.assertTrue(price_result.accepted)
        self.assertEqual(price_result.raw["predict_price_raw"], 42_500_000_000_000)
        self.assertEqual(len(bytes.fromhex(price_result.raw["instruction_data"])), 32)

        with self.assertRaises(MarketConfigurationError):
            self.adapter.place_paper_order(PaperOrderRequest("lamas_finance", yes_id, "SELL", 1))
        with self.assertRaises(MarketConfigurationError):
            self.adapter.place_paper_order(PaperOrderRequest("lamas_finance", yes_id, "BUY", 1, "not-a-number"))
        with self.assertRaises(MarketConfigurationError):
            self.adapter.place_paper_order(PaperOrderRequest("lamas_finance", yes_id, "BUY", 1e20))

    def test_unsupported_and_invalid_paths_fail_closed(self) -> None:
        with self.assertRaises(UnsupportedFeatureError):
            self.adapter.get_orderbook(f"{self.up_down_event}:YES")
        with self.assertRaises(UnsupportedFeatureError):
            self.adapter.copy_trade_from_activity({})
        with self.assertRaises(MarketConfigurationError):
            self.adapter.list_contracts("up_or_down:../outside")
        with self.assertRaises(MarketConfigurationError):
            self.adapter.get_price(f"{self.up_down_event}:MAYBE")

        bad_adapter = self.make_adapter()

        def bad_rpc(method: str, url: str, *, params=None, json_body=None, headers=None):
            if json_body["method"] == "getAccountInfo":
                payload = self._rpc(method, url, params=params, json_body=json_body, headers=headers)
                payload["result"]["value"]["owner"] = bad_adapter.price_program_id
                return payload
            return self._rpc(method, url, params=params, json_body=json_body, headers=headers)

        bad_adapter.runtime.request_json = bad_rpc  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            bad_adapter.list_contracts(self.up_down_event)

    def test_signed_transaction_forwarding_is_fail_closed(self) -> None:
        paper = self.adapter.place_paper_order(
            PaperOrderRequest("lamas_finance", f"{self.up_down_event}:YES", "BUY", 0.000001)
        )
        signed_bytes = (
            b"x" * 32
            + _base58_decode(self.adapter.up_or_down_program_id)
            + _base58_decode(self.fixture["program_accounts"]["up_or_down"][0]["pubkey"])
            + bytes.fromhex(paper.raw["instruction_data"])
        )
        signed_transaction = base64.b64encode(signed_bytes).decode("ascii")
        metadata = {
            "signed_transaction": signed_transaction,
            "program_id": self.adapter.up_or_down_program_id,
            "instruction": "predict",
            "round_account": self.fixture["program_accounts"]["up_or_down"][0]["pubkey"],
            "instruction_data": paper.raw["instruction_data"],
        }
        live_adapter = self.make_adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "lamas_finance_submit_signed_transactions": True,
            }
        )
        runtime_calls_before = len(self.calls)
        self.assertFalse(live_adapter.capabilities.live_trading)
        with self.assertRaises(UnsupportedFeatureError):
            live_adapter.place_live_order(
                PaperOrderRequest(
                    "lamas_finance",
                    f"{self.up_down_event}:YES",
                    "BUY",
                    0.000001,
                    metadata=metadata,
                )
            )
        self.assertEqual(len(self.calls), runtime_calls_before)

        with self.assertRaises(UnsupportedFeatureError):
            live_adapter.place_live_order(
                PaperOrderRequest(
                    "lamas_finance",
                    f"{self.up_down_event}:YES",
                    "BUY",
                    0.000001,
                    metadata={**metadata, "instruction_data": "00"},
                )
            )
        self.assertEqual(len(self.calls), runtime_calls_before)


if __name__ == "__main__":
    unittest.main()
