from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

from market_adapters.errors import MarketConfigurationError
from market_adapters.myriad import MyriadAdapter
from market_adapters.predict_fun import PredictFunAdapter
from market_adapters.types import PaperOrderRequest


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class PredictMyriadSignedBindingTests(unittest.TestCase):
    @staticmethod
    def _predict_payload() -> dict:
        return {
            "data": {
                "order": {
                    "hash": "0x" + "12" * 32,
                    "maker": "0x" + "11" * 20,
                    "signer": "0x" + "11" * 20,
                    "taker": "0x" + "00" * 20,
                    "tokenId": "111",
                    "makerAmount": "2800000000000000000",
                    "takerAmount": "5000000000000000000",
                    "side": 0,
                    "signature": "0x" + "ab" * 65,
                },
                "pricePerShare": "560000000000000000",
                "strategy": "LIMIT",
            }
        }

    @staticmethod
    def _predict_market() -> dict:
        return {
            "id": 9001,
            "outcomes": [
                {"name": "Yes", "indexSet": 1, "onChainId": "111"},
                {"name": "No", "indexSet": 2, "onChainId": "222"},
            ],
        }

    @staticmethod
    def _myriad_payload() -> dict:
        return {
            "order": {
                "trader": "0x" + "11" * 20,
                "marketId": "501",
                "outcomeId": 1,
                "side": 0,
                "amount": "20000000000000000000",
                "price": "620000000000000000",
                "minFillAmount": "0",
                "nonce": "1",
                "expiration": "0",
            },
            "signature": "0x" + "ab" * 65,
            "network_id": 56,
            "time_in_force": "GTC",
        }

    def test_predict_fun_matching_signed_terms_are_forwarded_unchanged(self) -> None:
        adapter = PredictFunAdapter(
            {"live_trading_enabled": True, "live_trading_confirmed": True}
        )
        payload = self._predict_payload()
        calls = []
        adapter.runtime.get_json = (  # type: ignore[method-assign]
            lambda url, *, params=None, headers=None: self._predict_market()
        )

        def request(method, url, *, json=None, headers=None, timeout=None):
            calls.append(json)
            return _Response({"success": True, "data": {"orderId": "predict-1"}})

        adapter.runtime.session.request = request  # type: ignore[method-assign]
        order = PaperOrderRequest(
            "predict_fun",
            "9001:YES",
            "BUY",
            5,
            0.56,
            {"predict_fun_order_payload": payload},
        )
        with patch.dict(os.environ, {"PREDICT_FUN_API_KEY": "predict-key"}):
            result = adapter.place_live_order(order)

        self.assertTrue(result["live"])
        self.assertEqual(calls, [payload])

    def test_predict_fun_rejects_signed_contract_side_price_and_size_mismatches(self) -> None:
        adapter = PredictFunAdapter(
            {"live_trading_enabled": True, "live_trading_confirmed": True}
        )
        adapter.runtime.get_json = (  # type: ignore[method-assign]
            lambda url, *, params=None, headers=None: self._predict_market()
        )
        sent = []
        adapter.runtime.session.request = (  # type: ignore[method-assign]
            lambda *args, **kwargs: sent.append(kwargs.get("json"))
        )
        mutations = {
            "strategy": lambda body: body["data"].update({"strategy": "MARKET"}),
            "fill_or_kill": lambda body: body["data"].update({"isFillOrKill": True}),
            "slippage": lambda body: body["data"].update({"slippageBps": 25}),
            "min_amount_out": lambda body: body["data"].update({"isMinAmountOut": True}),
            "market": lambda body: body["data"].update({"marketId": 9002}),
            "token": lambda body: body["data"]["order"].update({"tokenId": "222"}),
            "side": lambda body: body["data"]["order"].update({"side": 1}),
            "size": lambda body: body["data"]["order"].update(
                {
                    "makerAmount": "3360000000000000000",
                    "takerAmount": "6000000000000000000",
                }
            ),
            "price": lambda body: body["data"].update(
                {"pricePerShare": "570000000000000000"}
            ),
        }
        with patch.dict(os.environ, {"PREDICT_FUN_API_KEY": "predict-key"}):
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    payload = copy.deepcopy(self._predict_payload())
                    mutate(payload)
                    order = PaperOrderRequest(
                        "predict_fun",
                        "9001:YES",
                        "BUY",
                        5,
                        0.56,
                        {"predict_fun_order_payload": payload},
                    )
                    with self.assertRaises(MarketConfigurationError):
                        adapter.place_live_order(order)
        self.assertEqual(sent, [])

    def test_myriad_matching_signed_terms_are_forwarded_unchanged(self) -> None:
        adapter = MyriadAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "myriad_network_id": 56,
            }
        )
        payload = self._myriad_payload()
        calls = []

        def request(method, url, *, json=None, headers=None, timeout=None):
            calls.append(json)
            return _Response({"orderHash": "0x" + "34" * 32})

        adapter.runtime.session.request = request  # type: ignore[method-assign]
        order = PaperOrderRequest(
            "myriad_markets",
            "501:1",
            "BUY",
            20,
            0.62,
            {"myriad_order_payload": payload},
        )
        with patch.dict(
            os.environ,
            {"MYRIAD_API_KEY": "myriad-key", "MYRIAD_API_SECRET": "myriad-secret"},
        ):
            result = adapter.place_live_order(order)

        self.assertTrue(result["live"])
        self.assertEqual(calls, [payload])

    def test_myriad_rejects_signed_market_outcome_side_price_and_size_mismatches(self) -> None:
        adapter = MyriadAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "myriad_network_id": 56,
            }
        )
        sent = []
        adapter.runtime.session.request = (  # type: ignore[method-assign]
            lambda *args, **kwargs: sent.append(kwargs.get("json"))
        )
        mutations = {
            "network": lambda body: body.update({"network_id": 97}),
            "missing_network": lambda body: body.pop("network_id"),
            "market": lambda body: body["order"].update({"marketId": "502"}),
            "outcome": lambda body: body["order"].update({"outcomeId": 0}),
            "side": lambda body: body["order"].update({"side": 1}),
            "size": lambda body: body["order"].update(
                {"amount": "21000000000000000000"}
            ),
            "price": lambda body: body["order"].update(
                {"price": "630000000000000000"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                payload = copy.deepcopy(self._myriad_payload())
                mutate(payload)
                order = PaperOrderRequest(
                    "myriad_markets",
                    "501:1",
                    "BUY",
                    20,
                    0.62,
                    {"myriad_order_payload": payload},
                )
                with self.assertRaises(MarketConfigurationError):
                    adapter.place_live_order(order)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
