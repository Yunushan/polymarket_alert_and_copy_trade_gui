from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from polymarket import trader as trader_module


PRIVATE_KEY = "0x" + "1" * 64


class _SignedV2:
    salt = "1"
    maker = "0x" + "2" * 40
    signer = "0x" + "3" * 40
    tokenId = "token"
    makerAmount = "1"
    takerAmount = "1"
    side = "BUY"
    signatureType = 0
    timestamp = "1700000000000"
    metadata = "0x" + "0" * 64
    builder = "0x" + "0" * 64
    signature = "0x" + "4" * 130


class _V2Client:
    def __init__(self, *, version_response=None, built_order=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.version_response = {"version": 2} if version_response is None else version_response
        self.built_order = _SignedV2() if built_order is None else built_order

    def _get(self, endpoint):
        self.calls.append(("_get", endpoint))
        return self.version_response

    def create_order(self, order_args):
        self.calls.append(("create_order", order_args))
        return self.built_order

    def create_market_order(self, order_args):
        self.calls.append(("create_market_order", order_args))
        return self.built_order

    def post_order(self, signed_order, **kwargs):
        self.calls.append(("post_order", {"signed_order": signed_order, **kwargs}))
        return {"orderID": "0x" + "a" * 64}

    def get_balance_allowance(self, params):
        self.calls.append(("get_balance_allowance", params))
        return {"balance": "10", "allowance": "9"}

    def get_address(self):
        return "0x" + "3" * 40

    def cancel_order(self, payload):
        self.calls.append(("cancel_order", payload))
        return {"canceled": [payload.orderID], "not_canceled": {}}

    def cancel_orders(self, order_ids):
        self.calls.append(("cancel_orders", order_ids))
        return {"canceled": list(order_ids), "not_canceled": {}}

    def cancel_all(self):
        self.calls.append(("cancel_all", None))
        return {"canceled": ["all"], "not_canceled": {}}

    def cancel_market_orders(self, payload):
        self.calls.append(("cancel_market_orders", payload))
        return {"canceled": [payload.market], "not_canceled": {}}

    def post_orders(self, payloads, **kwargs):
        self.calls.append(("post_orders", {"payloads": payloads, **kwargs}))
        return [{"orderID": "0x" + "c" * 64}]

    def post_heartbeat(self, heartbeat_id=""):
        self.calls.append(("post_heartbeat", heartbeat_id))
        return {"heartbeat_id": heartbeat_id or "heartbeat-1"}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {"id": order_id}

    def get_open_orders(self, *, params, only_first_page, next_cursor):
        self.calls.append(
            (
                "get_open_orders",
                {"params": params, "only_first_page": only_first_page, "next_cursor": next_cursor},
            )
        )
        return []

    def get_trades(self, *, params, only_first_page, next_cursor):
        self.calls.append(
            (
                "get_trades",
                {"params": params, "only_first_page": only_first_page, "next_cursor": next_cursor},
            )
        )
        return []

    def is_order_scoring(self, params):
        self.calls.append(("is_order_scoring", params))
        return {"scoring": True}

    def get_builder_trades(self, params, *, next_cursor):
        self.calls.append(("get_builder_trades", {"params": params, "next_cursor": next_cursor}))
        return {"trades": [], "next_cursor": None}


class _InitializableClient:
    instances: list["_InitializableClient"] = []
    fail_derivation = False

    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.api_credentials = None
        self.derive_calls = 0
        self.create_calls = 0
        self.read_calls = []
        self.__class__.instances.append(self)

    def derive_api_key(self):
        self.derive_calls += 1
        if self.fail_derivation:
            raise RuntimeError("no existing credentials")
        return {"source": "derived"}

    def create_or_derive_api_key(self):
        self.create_calls += 1
        return {"source": "created"}

    def set_api_creds(self, credentials):
        self.api_credentials = credentials

    def get_open_orders(self, *, params, only_first_page, next_cursor):
        self.read_calls.append(
            {"params": params, "only_first_page": only_first_page, "next_cursor": next_cursor}
        )
        return []


class _CompatibilityReader:
    def get_order_by_id(self, order_id):
        return {"id": order_id}

    def get_orders(self, **filters):
        return filters

    def get_trades(self, **filters):
        return filters

    def get_order_status(self, order_id):
        return {"status": order_id}

    def get_builder_trades(self, **filters):
        return filters


class PolymarketTraderTests(unittest.TestCase):
    def _blocked_trader(self, *, reader=None) -> trader_module.PolymarketTrader:
        return trader_module.PolymarketTrader(
            trader_module.TraderConfig(private_key=PRIVATE_KEY),
            reader=reader,
        )

    def _enabled_trader(self, client: _V2Client | None = None) -> tuple[trader_module.PolymarketTrader, _V2Client]:
        mutation_client = client or _V2Client()
        instance = trader_module.PolymarketTrader(
            trader_module.TraderConfig(private_key=PRIVATE_KEY),
            mutation_client=mutation_client,
        )
        return instance, mutation_client

    def test_invalid_inputs_are_rejected_before_mutation_gate(self) -> None:
        instance = self._blocked_trader(reader=_V2Client())
        invalid_calls = (
            lambda: instance.place_limit_order(token_id="token", side="hold", price=0.5, size=1),
            lambda: instance.place_limit_order(token_id=" token", side="BUY", price=0.5, size=1),
            lambda: instance.place_limit_order(token_id="token", side="BUY", price=1, size=1),
            lambda: instance.place_limit_order(token_id="token", side="BUY", price=0.5, size=math.inf),
            lambda: instance.place_market_order_amount(token_id="token", side="", amount=1),
            lambda: instance.place_market_order_amount(token_id="token", side="BUY", amount=0),
        )
        for call in invalid_calls:
            with self.assertRaises(ValueError):
                call()

    def test_disabled_mutations_never_construct_or_expose_sdk_client(self) -> None:
        _InitializableClient.instances.clear()
        config = trader_module.TraderConfig(
            private_key=PRIVATE_KEY,
            funder_address="0x" + "2" * 40,
            signature_type=1,
        )
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", False),
        ):
            instance = trader_module.PolymarketTrader(config, mutation_client=_V2Client())
            with self.assertRaisesRegex(RuntimeError, "CLOB V2"):
                instance.place_limit_order(token_id="token", side="BUY", price=0.42, size=3)

        self.assertEqual(_InitializableClient.instances, [])
        self.assertFalse(hasattr(instance, "client"))
        with self.assertRaises(AttributeError):
            instance.client = object()

    def test_normal_product_and_bounded_audit_mutation_gates_are_independent(self) -> None:
        audit_client = _V2Client()
        with (
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True),
            patch.object(trader_module, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", False),
        ):
            audit = trader_module.PolymarketTrader(
                trader_module.TraderConfig(private_key=PRIVATE_KEY, bounded_audit=True),
                mutation_client=audit_client,
            )
            with self.assertRaisesRegex(RuntimeError, "bounded Polymarket CLOB V2 funded"):
                audit.place_limit_order(token_id="token", side="BUY", price=0.42, size=1)
        self.assertEqual(audit_client.calls, [])

        product_client = _V2Client()
        with (
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", False),
            patch.object(trader_module, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", True),
        ):
            product = trader_module.PolymarketTrader(
                trader_module.TraderConfig(private_key=PRIVATE_KEY),
                mutation_client=product_client,
            )
            with self.assertRaisesRegex(RuntimeError, "CLOB V2 mutations are disabled"):
                product.place_limit_order(token_id="token", side="BUY", price=0.42, size=1)
        self.assertEqual(product_client.calls, [])

    def test_v2_limit_and_market_orders_use_exact_guarded_types(self) -> None:
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            limit_response = instance.place_limit_order(
                token_id="token-1", side=" buy ", price=0.42, size=3, tif="GTC", post_only=True
            )
            market_response = instance.place_market_order_amount(
                token_id="token-2", side="SELL", amount=7, tif="FOK"
            )

        self.assertIn("orderID", limit_response)
        self.assertIn("orderID", market_response)
        version_calls = [value for name, value in client.calls if name == "_get"]
        self.assertEqual(version_calls, [f"{trader_module.CLOB_API}/version"] * 2)
        limit_args = next(value for name, value in client.calls if name == "create_order")
        self.assertEqual(limit_args.side, "BUY")
        self.assertEqual(limit_args.price, 0.42)
        market_args = next(value for name, value in client.calls if name == "create_market_order")
        self.assertEqual(market_args.side, "SELL")
        self.assertEqual(market_args.amount, 7)
        post_calls = [value for name, value in client.calls if name == "post_order"]
        self.assertEqual(post_calls[0]["order_type"], "GTC")
        self.assertTrue(post_calls[0]["post_only"])
        self.assertFalse(post_calls[0]["defer_exec"])
        self.assertEqual(post_calls[1]["order_type"], "FOK")
        self.assertFalse(post_calls[1]["post_only"])

    def test_v2_orders_reject_unreviewed_time_in_force_modes(self) -> None:
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            with self.assertRaisesRegex(ValueError, "exact TIF=GTC"):
                instance.place_limit_order(token_id="token", side="BUY", price=0.42, size=3, tif="FOK")
            with self.assertRaisesRegex(ValueError, "exact TIF=FOK"):
                instance.place_market_order_amount(token_id="token", side="BUY", amount=3, tif="GTC")
        self.assertEqual(client.calls, [])

    def test_v2_order_posting_fails_closed_on_server_version_or_legacy_build(self) -> None:
        cases = (
            (_V2Client(version_response={"version": 1}), "not V2"),
            (_V2Client(version_response={}), "invalid version response"),
            (_V2Client(built_order=object()), "unambiguous V2 signed order"),
        )
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            for client, message in cases:
                with self.subTest(message=message):
                    instance, _ = self._enabled_trader(client)
                    with self.assertRaisesRegex(RuntimeError, message):
                        instance.place_limit_order(
                            token_id="token",
                            side="BUY",
                            price=0.42,
                            size=1,
                            post_only=True,
                        )
                    self.assertFalse(any(name == "post_order" for name, _ in client.calls))

    def test_v2_balance_identity_cancellations_batch_and_heartbeat(self) -> None:
        order_one = "0x" + "a" * 64
        order_two = "0x" + "b" * 64
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            self.assertEqual(instance.get_trading_account_address(), "0x" + "3" * 40)
            self.assertEqual(instance.get_trading_balance_allowance(token_id="token", side="BUY")["balance"], "10")
            self.assertEqual(instance.cancel_order(order_one)["canceled"], [order_one])
            self.assertEqual(instance.cancel_orders([order_one, order_one, order_two])["canceled"], [order_one, order_two])
            self.assertEqual(instance.cancel_all_orders()["canceled"], ["all"])
            self.assertEqual(
                instance.cancel_market_orders("market-1", asset_id="token-1")["canceled"], ["market-1"]
            )
            batch = instance.place_multiple_orders([_SignedV2(), _SignedV2()])
            heartbeat = instance.send_heartbeat()
            next_heartbeat = instance.send_heartbeat(heartbeat["heartbeat_id"])

        self.assertEqual(batch[0]["orderID"], "0x" + "c" * 64)
        self.assertEqual(heartbeat, {"heartbeat_id": "heartbeat-1"})
        self.assertEqual(next_heartbeat, heartbeat)
        cancel_market_payload = next(value for name, value in client.calls if name == "cancel_market_orders")
        self.assertEqual(cancel_market_payload.market, "market-1")
        self.assertEqual(cancel_market_payload.asset_id, "token-1")
        batch_call = next(value for name, value in client.calls if name == "post_orders")
        self.assertEqual([item.orderType for item in batch_call["payloads"]], ["GTC", "GTC"])
        self.assertFalse(batch_call["post_only"])
        self.assertFalse(batch_call["defer_exec"])
        heartbeat_ids = [value for name, value in client.calls if name == "post_heartbeat"]
        self.assertEqual(heartbeat_ids, ["", "heartbeat-1"])

    def test_empty_mutation_collections_are_rejected(self) -> None:
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            with self.assertRaisesRegex(ValueError, "at least one order id"):
                instance.cancel_orders([])
            with self.assertRaisesRegex(ValueError, "at least one signed order"):
                instance.place_multiple_orders([])
        self.assertEqual(client.calls, [])

    def test_batch_and_cancel_hard_caps_and_v2_types_are_enforced_before_transport(self) -> None:
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            with self.assertRaisesRegex(ValueError, "at most 15"):
                instance.place_multiple_orders(_SignedV2() for _ in range(16))
            with self.assertRaisesRegex(RuntimeError, "unambiguous V2 signed order"):
                instance.place_multiple_orders([object()])
            with self.assertRaisesRegex(ValueError, "at most 3000"):
                instance.cancel_orders(f"order-{index}" for index in range(3001))
        self.assertFalse(any(name in {"post_orders", "cancel_orders"} for name, _ in client.calls))

    def test_v2_sdk_reads_use_exact_1_1_parameter_types(self) -> None:
        with patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True):
            instance, client = self._enabled_trader()
            self.assertEqual(instance.get_order("order-1"), {"id": "order-1"})
            self.assertEqual(
                instance.get_orders(
                    id="order-1",
                    market="market-1",
                    asset_id="asset-1",
                    only_first_page=True,
                    next_cursor="cursor-1",
                ),
                [],
            )
            self.assertEqual(
                instance.get_trades(
                    id="trade-1",
                    maker_address="0x" + "5" * 40,
                    market="market-1",
                    asset_id="asset-1",
                    before=20,
                    after=10,
                    only_first_page=True,
                ),
                [],
            )
            self.assertEqual(instance.get_order_scoring_status("order-1"), {"scoring": True})
            self.assertEqual(
                instance.get_builder_trades("builder-1", market="market-1", next_cursor="cursor-2"),
                {"trades": [], "next_cursor": None},
            )

        orders_call = next(value for name, value in client.calls if name == "get_open_orders")
        self.assertIsInstance(orders_call["params"], trader_module.OpenOrderParams)
        self.assertEqual(orders_call["params"].id, "order-1")
        self.assertTrue(orders_call["only_first_page"])
        self.assertEqual(orders_call["next_cursor"], "cursor-1")
        trades_call = next(value for name, value in client.calls if name == "get_trades")
        self.assertIsInstance(trades_call["params"], trader_module.TradeParams)
        self.assertEqual(trades_call["params"].before, 20)
        scoring = next(value for name, value in client.calls if name == "is_order_scoring")
        self.assertIsInstance(scoring, trader_module.OrderScoringParams)
        builder = next(value for name, value in client.calls if name == "get_builder_trades")
        self.assertIsInstance(builder["params"], trader_module.BuilderTradeParams)
        self.assertEqual(builder["next_cursor"], "cursor-2")

    def test_v2_initialization_uses_explicit_credentials_without_derivation(self) -> None:
        _InitializableClient.instances.clear()
        _InitializableClient.fail_derivation = False
        config = trader_module.TraderConfig(
            private_key=PRIVATE_KEY,
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="api-passphrase",
        )
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True),
        ):
            instance = trader_module.PolymarketTrader(config)

        client = _InitializableClient.instances[-1]
        self.assertEqual(client.init_args, (config.host,))
        self.assertEqual(client.init_kwargs["creds"].api_key, "api-key")
        self.assertEqual(client.derive_calls, 0)
        self.assertEqual(client.create_calls, 0)
        self.assertIs(client.api_credentials, client.init_kwargs["creds"])
        self.assertFalse(hasattr(instance, "client"))

    def test_read_only_sdk_uses_fresh_signed_reads_while_both_mutation_gates_are_disabled(self) -> None:
        _InitializableClient.instances.clear()
        config = trader_module.TraderConfig(
            private_key=PRIVATE_KEY,
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="api-passphrase",
            authenticated_sdk_reads=True,
        )
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", False),
            patch.object(trader_module, "POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED", False),
        ):
            instance = trader_module.PolymarketTrader(config)
            self.assertEqual(instance.get_orders(only_first_page=True), [])
            with self.assertRaisesRegex(RuntimeError, "CLOB V2 mutations are disabled"):
                instance.cancel_all_orders()

        client = _InitializableClient.instances[-1]
        self.assertEqual(client.derive_calls, 0)
        self.assertEqual(client.create_calls, 0)
        self.assertIs(client.api_credentials, client.init_kwargs["creds"])
        self.assertFalse(client.init_kwargs["retry_on_error"])
        self.assertEqual(len(client.read_calls), 1)
        self.assertIsInstance(client.read_calls[0]["params"], trader_module.OpenOrderParams)
        self.assertTrue(client.read_calls[0]["only_first_page"])
        self.assertIsNone(client.read_calls[0]["next_cursor"])

    def test_read_only_sdk_derivation_and_creation_are_separate_fail_closed_opt_ins(self) -> None:
        _InitializableClient.instances.clear()
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", False),
        ):
            with self.assertRaisesRegex(ValueError, "allow_api_key_derivation"):
                trader_module.PolymarketTrader(
                    trader_module.TraderConfig(
                        private_key=PRIVATE_KEY,
                        authenticated_sdk_reads=True,
                    )
                )
            self.assertEqual(_InitializableClient.instances, [])

            with self.assertRaisesRegex(ValueError, "forbids API-key creation"):
                trader_module.PolymarketTrader(
                    trader_module.TraderConfig(
                        private_key=PRIVATE_KEY,
                        authenticated_sdk_reads=True,
                        allow_api_key_creation=True,
                    )
                )
            self.assertEqual(_InitializableClient.instances, [])

            instance = trader_module.PolymarketTrader(
                trader_module.TraderConfig(
                    private_key=PRIVATE_KEY,
                    authenticated_sdk_reads=True,
                    allow_api_key_derivation=True,
                )
            )
            self.assertEqual(instance.get_orders(only_first_page=True), [])

        client = _InitializableClient.instances[-1]
        self.assertEqual(client.derive_calls, 1)
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.api_credentials, {"source": "derived"})

    def test_v2_initialization_derives_existing_credentials_without_creating(self) -> None:
        _InitializableClient.instances.clear()
        _InitializableClient.fail_derivation = False
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True),
        ):
            trader_module.PolymarketTrader(trader_module.TraderConfig(private_key=PRIVATE_KEY))

        client = _InitializableClient.instances[-1]
        self.assertEqual(client.derive_calls, 1)
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.api_credentials, {"source": "derived"})

    def test_v2_api_key_creation_requires_separate_opt_in(self) -> None:
        _InitializableClient.instances.clear()
        _InitializableClient.fail_derivation = True
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True),
        ):
            with self.assertRaisesRegex(RuntimeError, "separately approve"):
                trader_module.PolymarketTrader(trader_module.TraderConfig(private_key=PRIVATE_KEY))
            blocked_client = _InitializableClient.instances[-1]
            self.assertEqual(blocked_client.create_calls, 0)

            trader_module.PolymarketTrader(
                trader_module.TraderConfig(private_key=PRIVATE_KEY, allow_api_key_creation=True)
            )
            opted_in_client = _InitializableClient.instances[-1]
            self.assertEqual(opted_in_client.create_calls, 1)
            self.assertEqual(opted_in_client.api_credentials, {"source": "created"})
        _InitializableClient.fail_derivation = False

    def test_partial_explicit_api_credentials_are_rejected(self) -> None:
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", True),
        ):
            with self.assertRaisesRegex(ValueError, "require api_key, api_secret, and api_passphrase"):
                trader_module.PolymarketTrader(
                    trader_module.TraderConfig(private_key=PRIVATE_KEY, api_key="key-only")
                )

    def test_explicit_l2_headers_enable_reads_without_sdk_construction(self) -> None:
        _InitializableClient.instances.clear()
        headers = {
            "POLY_ADDRESS": "0x" + "2" * 40,
            "POLY_API_KEY": "api-key",
            "POLY_PASSPHRASE": "passphrase",
            "POLY_SIGNATURE": "signature",
            "POLY_TIMESTAMP": "1",
        }
        config = trader_module.TraderConfig(private_key="", l2_headers=headers)
        with (
            patch.object(trader_module, "ClobClient", _InitializableClient),
            patch.object(trader_module, "POLYMARKET_LIVE_MUTATIONS_SUPPORTED", False),
            patch.object(
                trader_module.clob_auth,
                "get_orders",
                return_value={"data": [], "next_cursor": "LTE="},
            ) as get_orders,
        ):
            instance = trader_module.PolymarketTrader(config)
            result = instance.get_orders(market="market-1")

        self.assertEqual(result, {"data": [], "next_cursor": "LTE="})
        self.assertEqual(_InitializableClient.instances, [])
        self.assertTrue(instance.auth_readiness["direct_l2_read_ready"])
        self.assertEqual(instance.get_trading_account_address(), headers["POLY_ADDRESS"])
        get_orders.assert_called_once_with(headers, market="market-1")

    def test_public_builder_read_uses_read_only_rest_surface(self) -> None:
        headers = {
            "POLY_ADDRESS": "0x" + "2" * 40,
            "POLY_API_KEY": "api-key",
            "POLY_PASSPHRASE": "passphrase",
            "POLY_SIGNATURE": "signature",
            "POLY_TIMESTAMP": "1",
        }
        instance = trader_module.PolymarketTrader(
            trader_module.TraderConfig(private_key="", l2_headers=headers)
        )
        with patch.object(
            trader_module.clob_rest,
            "get_builder_trades",
            return_value={"data": []},
        ) as get_builder_trades:
            result = instance.get_builder_trades("builder-1", market="market-1")

        self.assertEqual(result, {"data": []})
        get_builder_trades.assert_called_once_with("builder-1", market="market-1")

    def test_read_compatibility_wrappers_remain_narrow(self) -> None:
        instance = self._blocked_trader(reader=_CompatibilityReader())
        self.assertEqual(instance.get_order("order-1"), {"id": "order-1"})
        self.assertEqual(instance.get_orders(market="market-1"), {"market": "market-1"})
        self.assertEqual(instance.get_trades(asset_id="asset-1"), {"asset_id": "asset-1"})
        self.assertEqual(instance.get_order_scoring_status("order-1"), {"status": "order-1"})
        self.assertEqual(
            instance.get_builder_trades("builder-1", market="market-1"),
            {"builder_code": "builder-1", "market": "market-1"},
        )


if __name__ == "__main__":
    unittest.main()
