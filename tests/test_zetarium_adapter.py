from __future__ import annotations

import json
import unittest
from pathlib import Path

from eth_abi import encode

from market_adapters import PaperOrderRequest, ZetariumWorldAdapter
from market_adapters.errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zetarium_world" / "rpc_responses.json"


class ZetariumWorldAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.address = self.fixture["prediction_market_address"]
        self.adapter = ZetariumWorldAdapter({"zetarium_market_ids": [1, 2]})
        self.calls = []
        self.adapter.runtime.request_json = self._rpc  # type: ignore[method-assign]

    def _rpc(self, method: str, url: str, *, params=None, json_body=None, headers=None):
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://bsc-dataseed.binance.org")
        self.assertEqual(headers, {})
        self.calls.append(json_body)
        if json_body["method"] == "eth_sendRawTransaction":
            return {"jsonrpc": "2.0", "id": 1, "result": self.fixture["transaction_hash"]}
        call = json_body["params"][0]
        target = call["to"].lower()
        data = call["data"]
        selector = data[2:10]
        if selector == "406ef2ef":
            return {"result": "0x" + encode(["uint256"], [self.fixture["next_market_id"]]).hex()}
        if selector == "9619367d":
            return {"result": "0x" + encode(["uint256"], [self.fixture["min_bet"]]).hex()}
        if selector == "51ed6a30":
            return {"result": "0x" + "0" * 24 + self.fixture["stake_token"][2:].lower()}
        if target == self.fixture["stake_token"].lower() and selector == "313ce567":
            return {"result": "0x" + encode(["uint256"], [self.fixture["decimals"]]).hex()}
        if selector == "b1283e77":
            market_id = int(data[10:], 16)
            row = self.fixture["markets"][str(market_id)]
            values = [row[key] for key in ("id", "status", "outcome_count", "winning_outcome", "start_time", "end_time", "fee_bps", "creator", "total_pool", "exists")]
            return {"result": "0x" + encode(["uint256", "uint8", "uint8", "uint8", "uint64", "uint64", "uint16", "address", "uint256", "bool"], values).hex()}
        if selector == "26e5a7af":
            market_id = int(data[10:74], 16)
            outcome_id = int(data[74:138], 16)
            return {"result": "0x" + encode(["uint256"], [self.fixture["stakes"][f"{market_id}:{outcome_id}"]]).hex()}
        raise AssertionError(f"unexpected Zetarium RPC call: {json_body}")

    def test_health_events_contracts_and_pool_prices(self) -> None:
        health = self.adapter.health_check()
        self.assertEqual(health["chain_id"], 56)
        self.assertEqual(health["prediction_market_address"].lower(), self.address.lower())
        events = self.adapter.list_events()
        self.assertEqual([event.event_id.rsplit(":", 1)[1] for event in events], ["1", "2"])
        self.assertEqual(events[0].status, "open")
        contracts = self.adapter.list_contracts(events[0].event_id)
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        yes = self.adapter.get_price(contracts[0].contract_id)
        no = self.adapter.get_price(contracts[1].contract_id)
        self.assertAlmostEqual(yes.last or 0.0, 0.6)
        self.assertAlmostEqual(no.last or 0.0, 0.4)
        self.assertEqual(self.adapter.list_events("resolved"), [events[1]])

    def test_paper_order_is_bounded_and_builds_unsigned_calls(self) -> None:
        contract_id = f"{self.address.lower()}:1:0"
        result = self.adapter.place_paper_order(PaperOrderRequest("zetarium_world", contract_id, "BUY", 2, 0.6))
        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["amount_raw"], 2_000_000)
        self.assertEqual(result.raw["unsigned_place_bet_call"]["data"][:10], "0xda866c48")
        with self.assertRaises(MarketConfigurationError):
            self.adapter.place_paper_order(PaperOrderRequest("zetarium_world", contract_id, "SELL", 2, 0.6))
        with self.assertRaises(MarketConfigurationError):
            self.adapter.place_paper_order(PaperOrderRequest("zetarium_world", f"{self.address.lower()}:2:0", "BUY", 2, 0.6))
        with self.assertRaises(UnsupportedFeatureError):
            self.adapter.get_orderbook(contract_id)
        with self.assertRaises(UnsupportedFeatureError):
            self.adapter.copy_trade_from_activity({"side": "BUY"})

    def test_guarded_signed_live_order_requires_exact_reviewed_transaction(self) -> None:
        config = {
            "zetarium_market_ids": [1],
            "live_trading_enabled": True,
            "live_trading_confirmed": True,
            "zetarium_submit_signed_transactions": True,
            "zetarium_live_transaction_targets": [self.address],
        }
        adapter = ZetariumWorldAdapter(config)
        adapter.runtime.request_json = self._rpc  # type: ignore[method-assign]
        contract_id = f"{self.address.lower()}:1:0"
        metadata = {
            "signed_transaction": self.fixture["signed_transaction"],
            "chain_id": 56,
            "transaction_to": self.address,
            "transaction_data": self.fixture["transaction_data"],
            "transaction_value": 0,
            "market_id": 1,
            "outcome_id": 0,
            "side": "BUY",
            "size": 5,
            "limit_price": 0.5,
        }
        result = adapter.place_live_order(PaperOrderRequest("zetarium_world", contract_id, "BUY", 5, 0.5, metadata))
        self.assertTrue(result["live"])
        self.assertEqual(result["tx_hash"], self.fixture["transaction_hash"])
        self.assertNotIn(self.fixture["signed_transaction"], json.dumps(result))
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(
                PaperOrderRequest("zetarium_world", contract_id, "BUY", 5, 0.5, {**metadata, "outcome_id": 1})
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(
                PaperOrderRequest("zetarium_world", contract_id, "BUY", 5, 0.5, {**metadata, "transaction_value": 1})
            )

    def test_empty_pool_price_is_fail_closed(self) -> None:
        adapter = ZetariumWorldAdapter({"zetarium_market_ids": [1]})

        def empty_rpc(method: str, url: str, *, params=None, json_body=None, headers=None):
            payload = self._rpc(method, url, params=params, json_body=json_body, headers=headers)
            if json_body["method"] == "eth_call" and json_body["params"][0]["data"][2:10] == "26e5a7af":
                return {"result": "0x" + "0" * 64}
            return payload

        adapter.runtime.request_json = empty_rpc  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.get_price(f"{self.address.lower()}:1:0")


if __name__ == "__main__":
    unittest.main()
