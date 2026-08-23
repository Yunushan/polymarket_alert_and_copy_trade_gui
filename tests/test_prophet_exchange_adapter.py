from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import PaperOrderRequest, ProphetExchangeAdapter, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "prophet_exchange"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class ProphetExchangeAdapterTests(unittest.TestCase):
    def _market_data_adapter(self, **config):
        settings = {
            "prophet_exchange_api_key": "api-key",
            "prophet_exchange_api_base_url": "https://api.test/partner",
        }
        settings.update(config)
        adapter = ProphetExchangeAdapter(settings)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            self.assertEqual(headers, {"Authorization": "api-key"})
            if url.endswith("/affiliate/get_sport_events"):
                return load_fixture("sport_events")
            if url.endswith("/v3/affiliate/get_markets"):
                self.assertEqual(params, {"event_id": 101})
                return load_fixture("markets")
            raise AssertionError(f"unexpected ProphetX URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_health_and_documented_capabilities_are_explicit(self) -> None:
        adapter, _ = self._market_data_adapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "prophet_exchange")
        self.assertEqual(health["api_version"], "v3")
        self.assertTrue(health["market_data_api_key_required"])
        self.assertTrue(health["trading_api_credentials_required"])
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertEqual(
            health["account_recovery_operations"],
            ["balance", "transactions", "order_history", "order_detail", "trades"],
        )
        self.assertEqual(health["order_management_operations"], ["cancel_order", "cancel_orders"])
        self.assertEqual(health["order_management_endpoints"], ["POST /mm/cancel_order", "POST /mm/cancel_multiple_orders"])

    def test_market_data_contracts_quotes_and_paper_order(self) -> None:
        adapter, calls = self._market_data_adapter()
        order = PaperOrderRequest(
            "prophet_exchange",
            "101:555:1:line_1",
            "BUY",
            5,
            0.5,
        )

        events = adapter.list_events("patriots")
        contracts = adapter.list_contracts("101")
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id, "101")
        self.assertEqual(events[0].title, "Patriots vs. Jets")
        self.assertEqual(
            [contract.contract_id for contract in contracts],
            ["101:555:1:line_1", "101:555:2:line_2", "101:556:3:total_over", "101:556:4:total_under"],
        )
        self.assertAlmostEqual(book.asks[0].price, 1 / 1.95)
        self.assertEqual(book.asks[0].size, 2100.0)
        self.assertAlmostEqual(price.ask or 0.0, 1 / 1.95)
        self.assertEqual(price.source, "prophetx_affiliate_market_data")
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["strike_id"], "strike_1")
        self.assertEqual(paper.raw["request"]["price"], 2.0)
        self.assertGreaterEqual(len(calls), 5)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({})

    def test_authenticated_trades_and_derived_candles_use_fixed_v4_contracts(self) -> None:
        adapter = ProphetExchangeAdapter(
            {
                "prophet_exchange_access_token": "fixture-access-token",
                "prophet_exchange_api_base_url": "https://api.test/partner",
            }
        )
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            self.assertEqual(headers, {"Authorization": "fixture-access-token"})
            if url.endswith("/v4/mm/get_trades"):
                self.assertEqual(params.get("from"), 1786370400)
                self.assertEqual(params.get("to"), 1786370401)
                self.assertIn(params.get("limit"), {10, 100})
                return load_fixture("trades")
            if url.endswith("/v4/mm/get_order/order-filled-1"):
                return load_fixture("order_filled")
            if url.endswith("/v4/mm/get_order/order-other-contract"):
                return load_fixture("order_other_contract")
            if url.endswith("/v3/affiliate/get_markets"):
                self.assertEqual(params, {"event_id": 101})
                return load_fixture("markets")
            raise AssertionError(f"unexpected ProphetX history URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        trades = adapter.list_trades(
            "101:555:1:line_1",
            limit=10,
            after=1786370400,
            before=1786370401,
        )
        self.assertEqual([trade.trade_id for trade in trades], ["7001"])
        self.assertEqual(trades[0].side, "BUY")
        self.assertAlmostEqual(trades[0].price, 0.5)
        self.assertEqual(trades[0].size, 5.0)
        self.assertEqual(trades[0].raw["order"]["strike_id"], "strike_1")

        candles = adapter.list_candles(
            "101:555:1:line_1",
            resolution="1h",
            from_timestamp=1786370400,
            to_timestamp=1786370401,
        )
        self.assertEqual(len(candles), 1)
        self.assertAlmostEqual(candles[0].open, 0.5)
        self.assertAlmostEqual(candles[0].close, 0.5)
        self.assertEqual(candles[0].volume, 5.0)
        self.assertEqual(candles[0].raw["trade_ids"], ["7001"])
        self.assertGreaterEqual(len(calls), 6)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("101:555:1:line_1", resolution="2h")

    def test_live_order_uses_guarded_trading_api_shape(self) -> None:
        adapter = ProphetExchangeAdapter(
            {
                "prophet_exchange_access_token": "fixture-access-token",
                "prophet_exchange_api_base_url": "https://api.test/partner",
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {"Authorization": "fixture-access-token"})
            self.assertTrue(url.endswith("/v3/affiliate/get_markets"))
            self.assertEqual(params, {"event_id": 101})
            return load_fixture("markets")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, dict(json_body or {}), dict(headers or {})))
            self.assertEqual(method, "POST")
            self.assertEqual(headers["Authorization"], "fixture-access-token")
            self.assertTrue(url.endswith("/mm/submit_order"))
            return load_fixture("order_response")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        result = adapter.place_live_order(
            PaperOrderRequest("prophet_exchange", "101:555:1:line_1", "BUY", 5, 0.5)
        )

        self.assertTrue(result["live"])
        self.assertEqual(result["response"]["data"]["order"]["status"], "accepted")
        self.assertEqual(calls[0][2]["strike_id"], "strike_1")
        self.assertEqual(calls[0][2]["price"], 2.0)
        self.assertEqual(calls[0][2]["quantity"], 5.0)

    def test_contract_and_order_validation_rejects_unsafe_inputs(self) -> None:
        adapter, _ = self._market_data_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.get_price("../../private:555:1:line_1")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest("prophet_exchange", "101:555:1:line_1", "SELL", 1, 0.5)
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest("prophet_exchange", "101:555:1:line_1", "BUY", 1, 0.0)
            )

    def test_account_recovery_and_guarded_cancellation(self) -> None:
        adapter = ProphetExchangeAdapter(
            {
                "prophet_exchange_access_token": "fixture-access-token",
                "prophet_exchange_api_base_url": "https://api.test/partner",
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "prophet_exchange_order_management_enabled": True,
            }
        )
        read_calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            read_calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/v4/mm/get_balance"):
                return load_fixture("balance")
            if url.endswith("/v4/mm/get_transactions"):
                return load_fixture("transactions")
            if url.endswith("/v4/mm/get_order_history"):
                return {"data": {"next_cursor": "", "orders": []}}
            if url.endswith("/v4/mm/get_order/order-filled-1"):
                return load_fixture("order_filled")
            if url.endswith("/v4/mm/get_trades"):
                return load_fixture("trades")
            raise AssertionError(f"unexpected ProphetX account URL: {url}")

        mutation_calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            mutation_calls.append((method, url, dict(json_body or {}), dict(headers or {})))
            if url.endswith("/mm/cancel_order"):
                return load_fixture("cancel_response")
            if url.endswith("/mm/cancel_multiple_orders"):
                return load_fixture("cancel_multiple_response")
            raise AssertionError(f"unexpected ProphetX mutation URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        balance = adapter.account_recovery("balance")
        transactions = adapter.account_recovery("transactions", cursor="41", limit=25)
        self.assertEqual(balance["data"]["balance"], 1000.0)
        self.assertEqual(transactions["next"], 42)
        self.assertEqual(read_calls[0][1], {})
        self.assertEqual(read_calls[1][1], {"next_cursor": "41", "limit": 25})

        order_history = adapter.account_recovery(
            "order_history",
            cursor="next-1",
            limit=10,
            market_id="555",
            event_id="101",
            matching_status="fully_matched",
            status="settled",
            after=1786370400,
            before=1786370401,
        )
        order_detail = adapter.account_recovery("order_detail", order_id="order-filled-1")
        account_trades = adapter.account_recovery("trades", cursor="next-2", limit=20)
        self.assertEqual(order_history["data"]["orders"], [])
        self.assertEqual(order_detail["data"]["order_id"], "order-filled-1")
        self.assertEqual(account_trades["data"]["trades"][0]["id"], 7001)
        self.assertEqual(
            read_calls[2][1],
            {
                "next_cursor": "next-1",
                "from": 1786370400,
                "to": 1786370401,
                "limit": 10,
                "matching_status": "fully_matched",
                "status": "settled",
                "market_id": "555",
                "event_id": "101",
            },
        )
        self.assertEqual(read_calls[3][0].split("/v4/mm/")[-1], "get_order/order-filled-1")
        self.assertEqual(read_calls[4][1], {"next_cursor": "next-2", "limit": 20})

        single = adapter.manage_orders(
            "cancel_order",
            order_id="order-1",
            external_id="external-1",
            confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        batch = adapter.manage_orders(
            "cancel_orders",
            orders=[
                {"order_id": "order-1", "external_id": "external-1"},
                {"order_id": "order-2", "external_id": "external-2"},
            ],
            confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        self.assertEqual(single["response"]["data"]["order"]["status"], "cancelled")
        self.assertEqual(batch["response"]["data"]["failed_orders"], [])
        self.assertEqual(mutation_calls[0][2], {"external_id": "external-1", "order_id": "order-1"})
        self.assertEqual(
            mutation_calls[1][2],
            {
                "data": [
                    {"external_id": "external-1", "order_id": "order-1"},
                    {"external_id": "external-2", "order_id": "order-2"},
                ]
            },
        )

        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_order",
                order_id="order-1",
                external_id="../unsafe",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_orders", orders=[], confirm_order_management="wrong")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("transactions", cursor="-1")


if __name__ == "__main__":
    unittest.main()
