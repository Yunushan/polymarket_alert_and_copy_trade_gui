from __future__ import annotations

import json
import unittest
from typing import Any

from market_adapters import ContextV2Adapter, PaperOrderRequest, ProbableAdapter
from market_adapters.errors import MarketConfigurationError


class _FakeResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class SignedOrderBindingTests(unittest.TestCase):
    @staticmethod
    def _configure_probable_market_lookup(adapter: ProbableAdapter) -> None:
        markets = {
            "market-1": {
                "id": "market-1",
                "tokens": [
                    {"token_id": "token-yes", "outcome": "Yes"},
                    {"token_id": "token-no", "outcome": "No"},
                ],
            },
            "market-2": {
                "id": "market-2",
                "tokens": [{"token_id": "token-other", "outcome": "Yes"}],
            },
        }

        def fake_get_json(url: str, *, params=None, headers=None):
            del params, headers
            market_id = url.rstrip("/").rsplit("/", 1)[-1]
            return markets.get(market_id, {})

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

    def test_context_rejects_signed_economic_fields_that_differ_from_preflight(self) -> None:
        adapter = ContextV2Adapter(
            {
                "context_api_key": "context-key",
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )
        calls: list[dict[str, Any]] = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append(dict(json or {}))
            return _FakeResponse({"success": True})

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        market_id = "0x" + "ab" * 32
        signed_order = {
            "type": "limit",
            "marketId": market_id,
            "outcomeIndex": 0,
            "side": 0,
            "price": "440000",
            "size": "5000000",
            "trader": "0x" + "12" * 20,
            "nonce": "0x1",
            "signature": "0x" + "ab" * 65,
        }
        order = PaperOrderRequest(
            "context_v2",
            f"{market_id}:0",
            "BUY",
            5,
            0.44,
            {"signed_order": signed_order},
        )

        result = adapter.place_live_order(order)

        self.assertTrue(result["live"])
        self.assertEqual(
            {field: calls[0][field] for field in signed_order},
            signed_order,
        )

        mismatches = {
            "marketId": "0x" + "cd" * 32,
            "outcomeIndex": 1,
            "side": 1,
            "price": "450000",
            "size": "6000000",
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                tampered = {**signed_order, field: value}
                with self.assertRaisesRegex(MarketConfigurationError, "does not match"):
                    adapter.place_live_order(
                        PaperOrderRequest(
                            "context_v2",
                            order.contract_id,
                            "BUY",
                            5,
                            0.44,
                            {"signed_order": tampered},
                        )
                    )
        for field in ("marketId", "outcomeIndex", "side", "price", "size"):
            with self.subTest(missing=field):
                incomplete = dict(signed_order)
                incomplete.pop(field)
                with self.assertRaisesRegex(MarketConfigurationError, "missing preflight-bound"):
                    adapter.place_live_order(
                        PaperOrderRequest(
                            "context_v2",
                            order.contract_id,
                            "BUY",
                            5,
                            0.44,
                            {"signed_order": incomplete},
                        )
                    )
        malformed = {**signed_order, "signature": "0xsigned"}
        with self.assertRaisesRegex(MarketConfigurationError, "hexadecimal"):
            adapter.place_live_order(
                PaperOrderRequest(
                    "context_v2",
                    order.contract_id,
                    "BUY",
                    5,
                    0.44,
                    {"signed_order": malformed},
                )
            )
        self.assertEqual(len(calls), 1)

    def test_probable_rejects_signed_token_side_price_size_and_outer_market_drift(self) -> None:
        adapter = ProbableAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "probable_address": "0x" + "11" * 20,
                "probable_api_key": "prob-key",
                "probable_api_secret": "c2VjcmV0",
                "probable_api_passphrase": "prob-pass",
            }
        )
        calls: list[dict[str, Any]] = []
        self._configure_probable_market_lookup(adapter)

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append(json.loads(data))
            return _FakeResponse({"orderId": 123})

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        signed_order = {
            "salt": "1",
            "maker": "0x" + "11" * 20,
            "signer": "0x" + "11" * 20,
            "taker": "0x" + "00" * 20,
            "tokenId": "token-yes",
            "makerAmount": "2200000000000000000",
            "takerAmount": "5000000000000000000",
            "expiration": "0",
            "nonce": "0",
            "feeRateBps": "0",
            "side": 0,
            "signatureType": 0,
            "signature": "0x" + "ab" * 65,
        }
        order = PaperOrderRequest(
            "probable",
            "market-1:token-yes",
            "BUY",
            5,
            0.44,
            {"signed_order": signed_order},
        )

        result = adapter.place_live_order(order)

        self.assertEqual(result["response"]["orderId"], 123)
        self.assertEqual(calls[0]["order"], signed_order)

        mismatches = (
            {**signed_order, "tokenId": "token-no"},
            {**signed_order, "side": 1},
            {**signed_order, "makerAmount": "2250000000000000000"},
            {
                **signed_order,
                "makerAmount": "4400000000000000000",
                "takerAmount": "10000000000000000000",
            },
        )
        for tampered in mismatches:
            with self.subTest(tampered=tampered):
                with self.assertRaisesRegex(MarketConfigurationError, "match"):
                    adapter.place_live_order(
                        PaperOrderRequest(
                            "probable",
                            order.contract_id,
                            "BUY",
                            5,
                            0.44,
                            {"signed_order": tampered},
                        )
                    )

        with self.assertRaisesRegex(MarketConfigurationError, "does not contain token or outcome"):
            adapter.place_live_order(
                PaperOrderRequest(
                    "probable",
                    "market-2:token-yes",
                    "BUY",
                    5,
                    0.44,
                    {"signed_order": signed_order},
                )
            )

        for malformed_signature in (
            "0xsigned",
            "0x" + "ab" * 64,
            "0x" + "ab" * 66,
        ):
            with self.subTest(signature=malformed_signature):
                with self.assertRaisesRegex(MarketConfigurationError, "65-byte"):
                    adapter.place_live_order(
                        PaperOrderRequest(
                            "probable",
                            order.contract_id,
                            "BUY",
                            5,
                            0.44,
                            {
                                "signed_order": {
                                    **signed_order,
                                    "signature": malformed_signature,
                                }
                            },
                        )
                    )

        with self.assertRaisesRegex(MarketConfigurationError, "does not match"):
            adapter.place_live_order(
                PaperOrderRequest(
                    "probable",
                    order.contract_id,
                    "BUY",
                    5,
                    0.44,
                    {
                        "probable_payload": {
                            "marketId": "market-2",
                            "owner": signed_order["signer"],
                            "orderType": "GTC",
                            "order": signed_order,
                        }
                    },
                )
            )
        self.assertEqual(len(calls), 1)

    def test_probable_accepts_matching_chain_native_signed_amounts(self) -> None:
        adapter = ProbableAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "probable_address": "0x" + "11" * 20,
                "probable_api_key": "prob-key",
                "probable_api_secret": "c2VjcmV0",
                "probable_api_passphrase": "prob-pass",
            }
        )
        calls: list[dict[str, Any]] = []
        self._configure_probable_market_lookup(adapter)

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append(json.loads(data))
            return _FakeResponse({"orderId": 456})

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        signed_order = {
            "maker": "0x" + "11" * 20,
            "signer": "0x" + "11" * 20,
            "tokenId": "token-yes",
            "makerAmount": "2200000000000000000",
            "takerAmount": "5000000000000000000",
            "side": "BUY",
            "signature": "0x" + "ab" * 65,
        }

        adapter.place_live_order(
            PaperOrderRequest(
                "probable",
                "market-1:YES",
                "BUY",
                5,
                0.44,
                {"signed_order": signed_order},
            )
        )

        self.assertEqual(calls[0]["order"], signed_order)

    def test_probable_non_mainnet_amount_scale_must_be_explicit(self) -> None:
        settings = {
            "live_trading_enabled": True,
            "live_trading_confirmed": True,
            "probable_chain_id": 97,
            "probable_address": "0x" + "11" * 20,
            "probable_api_key": "prob-key",
            "probable_api_secret": "c2VjcmV0",
            "probable_api_passphrase": "prob-pass",
        }
        signed_order = {
            "maker": "0x" + "11" * 20,
            "signer": "0x" + "11" * 20,
            "tokenId": "token-yes",
            "makerAmount": "2200000",
            "takerAmount": "5000000",
            "side": 0,
            "signature": "0x" + "ab" * 65,
        }
        order = PaperOrderRequest(
            "probable",
            "market-1:token-yes",
            "BUY",
            5,
            0.44,
            {"signed_order": signed_order},
        )

        without_scale = ProbableAdapter(settings)
        self._configure_probable_market_lookup(without_scale)
        with self.assertRaisesRegex(MarketConfigurationError, "explicitly configured"):
            without_scale.place_live_order(order)

        calls: list[dict[str, Any]] = []
        adapter = ProbableAdapter({**settings, "probable_amount_decimals": 6})
        self._configure_probable_market_lookup(adapter)

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append(json.loads(data))
            return _FakeResponse({"orderId": 789})

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        adapter.place_live_order(order)
        self.assertEqual(calls[0]["order"], signed_order)


if __name__ == "__main__":
    unittest.main()
