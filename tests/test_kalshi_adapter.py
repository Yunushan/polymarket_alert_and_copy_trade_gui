from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import KalshiAdapter, PaperOrderRequest
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kalshi"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class KalshiAdapterTests(unittest.TestCase):
    def make_adapter(self) -> KalshiAdapter:
        adapter = KalshiAdapter()
        markets = load_fixture("markets")
        orderbook = load_fixture("orderbook")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/markets"):
                event_ticker = (params or {}).get("event_ticker")
                if event_ticker:
                    filtered = [
                        market
                        for market in markets["markets"]
                        if market.get("event_ticker") == event_ticker
                    ]
                    return {"markets": filtered, "cursor": ""}
                return markets
            if url.endswith("/markets/KXFED-26MAY-TARGET-425"):
                return {"market": markets["markets"][0]}
            if url.endswith("/markets/KXFED-26MAY-TARGET-425/orderbook"):
                return orderbook
            if url.endswith("/markets/trades"):
                return load_fixture("trades")
            if url.endswith("/series/KXFED/markets/KXFED-26MAY-TARGET-425/candlesticks"):
                return load_fixture("candlesticks")
            raise AssertionError(f"unexpected Kalshi URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_registered_metadata_advertises_supported_kalshi_features(self) -> None:
        adapter = KalshiAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "kalshi")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertIn("external-api.kalshi.com", health["api_base_url"])

    def test_list_events_groups_markets_by_event_and_filters_query(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("fed", limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "KXFED-26MAY")
        self.assertEqual(events[0].market_id, "kalshi")
        self.assertEqual(events[0].status, "active")
        self.assertEqual(len(events[0].raw["markets"]), 2)

    def test_list_contracts_creates_yes_and_no_contracts(self) -> None:
        adapter = self.make_adapter()

        contracts = adapter.list_contracts("KXFED-26MAY")

        self.assertEqual(len(contracts), 4)
        self.assertEqual(contracts[0].contract_id, "KXFED-26MAY-TARGET-425:YES")
        self.assertEqual(contracts[1].contract_id, "KXFED-26MAY-TARGET-425:NO")
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertEqual(contracts[1].outcome, "No")

    def test_orderbook_converts_opposite_side_bids_to_asks(self) -> None:
        adapter = self.make_adapter()

        book = adapter.get_orderbook("KXFED-26MAY-TARGET-425:YES")

        self.assertEqual([level.price for level in book.bids], [0.41, 0.39])
        self.assertEqual([level.price for level in book.asks], [0.42, 0.44])
        self.assertEqual([level.size for level in book.asks], [2.0, 7.0])

    def test_no_side_orderbook_and_price_are_supported(self) -> None:
        adapter = self.make_adapter()

        book = adapter.get_orderbook("KXFED-26MAY-TARGET-425:NO")
        price = adapter.get_price("KXFED-26MAY-TARGET-425:NO")

        self.assertEqual([level.price for level in book.bids], [0.58, 0.56])
        self.assertEqual([level.price for level in book.asks], [0.59, 0.61])
        self.assertAlmostEqual(price.bid or 0, 0.58)
        self.assertAlmostEqual(price.ask or 0, 0.59)
        self.assertAlmostEqual(price.midpoint or 0, 0.585)

    def test_public_trade_history_is_filtered_and_normalized(self) -> None:
        adapter = self.make_adapter()

        yes_trades = adapter.list_trades(
            "KXFED-26MAY-TARGET-425:YES",
            limit=25,
            after=1777630000,
            before=1777640000,
        )

        self.assertEqual(len(yes_trades), 1)
        self.assertEqual(yes_trades[0].trade_id, "trade-kalshi-1")
        self.assertEqual(yes_trades[0].side, "YES")
        self.assertAlmostEqual(yes_trades[0].price, 0.42)
        self.assertAlmostEqual(yes_trades[0].size, 12.0)
        self.assertIsNotNone(yes_trades[0].timestamp)

    def test_public_candles_are_normalized_and_no_prices_are_complemented(self) -> None:
        adapter = self.make_adapter()

        yes = adapter.list_candles(
            "KXFED-26MAY-TARGET-425:YES",
            resolution="1h",
            from_timestamp=1777630000,
            to_timestamp=1777640000,
        )
        no = adapter.list_candles(
            "KXFED-26MAY-TARGET-425:NO",
            resolution="1h",
            from_timestamp=1777630000,
            to_timestamp=1777640000,
        )

        self.assertEqual(len(yes), 1)
        self.assertEqual(yes[0].timestamp, 1777636800.0)
        self.assertEqual((yes[0].open, yes[0].high, yes[0].low, yes[0].close), (0.4, 0.45, 0.39, 0.42))
        self.assertEqual((no[0].open, no[0].high, no[0].low, no[0].close), (0.6, 0.61, 0.55, 0.58))
        self.assertEqual(yes[0].volume, 125.0)

    def test_authenticated_account_reads_use_allow_listed_signed_endpoints(self) -> None:
        adapter = KalshiAdapter()
        fixtures = {
            "/portfolio/orders": load_fixture("account_orders"),
            "/historical/orders": load_fixture("account_orders"),
            "/portfolio/fills": load_fixture("account_fills"),
            "/historical/fills": load_fixture("account_fills"),
            "/portfolio/positions": load_fixture("account_positions"),
            "/portfolio/settlements": load_fixture("account_settlements"),
            "/portfolio/balance": load_fixture("account_balance"),
            "/portfolio/orders/queue_positions": load_fixture("account_queue_positions"),
        }
        calls = []

        class Response:
            status_code = 200
            text = ""

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        adapter._auth_headers = lambda method, path: {  # type: ignore[method-assign]
            "KALSHI-ACCESS-KEY": "test-key",
            "KALSHI-ACCESS-SIGNATURE": "test-signature",
            "KALSHI-ACCESS-TIMESTAMP": "123",
        }

        def fake_request(method, url, *, params=None, headers=None, timeout=None):
            path = url.split("/trade-api/v2", 1)[-1]
            calls.append((method, path, dict(params or {}), dict(headers or {})))
            return Response(fixtures[path])

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]

        active = adapter.account_recovery(
            "active_orders",
            ticker="KXFED-26MAY-TARGET-425",
            limit=10,
            cursor="next",
            subaccount=0,
        )
        fills = adapter.account_recovery(
            "fills",
            ticker="KXFED-26MAY-TARGET-425",
            order_id="order-kalshi-1",
            historical=True,
            min_timestamp=1777630000,
            max_timestamp=1777640000,
        )
        positions = adapter.account_recovery(
            "positions",
            event_ticker="KXFED-26MAY",
            count_filter="position,total_traded",
        )
        settlements = adapter.account_recovery("settlements", event_ticker="KXFED-26MAY")
        balance = adapter.account_recovery("balance", subaccount=0)
        queue = adapter.account_recovery("queue_positions", ticker="KXFED-26MAY-TARGET-425")
        copy_preview = adapter.copy_trade_from_activity(fills["fills"][0])

        self.assertEqual(active["orders"][0]["order_id"], "order-kalshi-1")
        self.assertEqual(fills["fills"][0]["fill_id"], "fill-kalshi-1")
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "KXFED-26MAY-TARGET-425:YES")
        self.assertEqual(copy_preview.raw["source"], "kalshi_authenticated_portfolio_fills")
        self.assertEqual(copy_preview.raw["ticker"], "KXFED-26MAY-TARGET-425")
        self.assertAlmostEqual(copy_preview.average_price or 0.0, 0.42)
        self.assertEqual(positions["market_positions"][0]["position_fp"], "2.00")
        self.assertEqual(settlements["settlements"][0]["market_result"], "yes")
        self.assertEqual(balance["balance"], 123.45)
        self.assertEqual(queue["queue_positions"][0]["queue_position_fp"], "4.00")

        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**fills["fills"][0], "book_side": "unknown"})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**fills["fills"][0], "count_fp": "0"})
        self.assertEqual(calls[0][1], "/portfolio/orders")
        self.assertEqual(calls[0][2]["status"], "resting")
        self.assertEqual(calls[1][1], "/historical/fills")
        self.assertEqual(calls[1][2]["order_id"], "order-kalshi-1")
        self.assertEqual(calls[2][2]["count_filter"], "position,total_traded")
        self.assertTrue(all(call[3]["KALSHI-ACCESS-SIGNATURE"] == "test-signature" for call in calls))

    def test_account_reads_fail_closed_on_invalid_parameters_or_operations(self) -> None:
        adapter = KalshiAdapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("order_history", status="deleted")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("positions", count_filter="position,unexpected")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("queue_positions")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("arbitrary")

    def test_history_validation_rejects_invalid_ranges(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("KXFED-26MAY-TARGET-425:YES", limit=1001)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("KXFED-26MAY-TARGET-425:YES", resolution="15m")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                "KXFED-26MAY-TARGET-425:YES",
                from_timestamp=1777640000,
                to_timestamp=1777630000,
            )

    def test_paper_order_is_dry_run_and_validates_input(self) -> None:
        adapter = self.make_adapter()
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="kalshi",
                contract_id="KXFED-26MAY-TARGET-425:YES",
                side="BUY",
                size=3,
                limit_price=0.42,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertEqual(result.contract_id, "KXFED-26MAY-TARGET-425:YES")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="kalshi",
                    contract_id="KXFED-26MAY-TARGET-425:MAYBE",
                    side="BUY",
                    size=3,
                    limit_price=0.42,
                )
            )

    def test_live_trading_is_disabled_by_default(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="kalshi",
                    contract_id="KXFED-26MAY-TARGET-425:YES",
                    side="BUY",
                    size=3,
                    limit_price=0.42,
                )
            )

        self.assertIn("disabled", str(ctx.exception))

    def test_live_order_payload_maps_no_contract_to_yes_side_book(self) -> None:
        adapter = self.make_adapter()
        payload = adapter._build_live_order_payload(
            PaperOrderRequest(
                market_id="kalshi",
                contract_id="KXFED-26MAY-TARGET-425:NO",
                side="BUY",
                size=2,
                limit_price=0.35,
                metadata={"client_order_id": "client-1"},
            )
        )

        self.assertEqual(payload["ticker"], "KXFED-26MAY-TARGET-425")
        self.assertEqual(payload["client_order_id"], "client-1")
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["count"], "2.00")
        self.assertEqual(payload["price"], "0.6500")

    def test_live_trading_requires_credentials_when_enabled(self) -> None:
        adapter = KalshiAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="kalshi",
                    contract_id="KXFED-26MAY-TARGET-425:YES",
                    side="BUY",
                    size=1,
                    limit_price=0.5,
                )
            )

        self.assertIn("KALSHI_API_KEY_ID", str(ctx.exception))

    def test_live_order_signs_and_posts_with_generated_credentials(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        adapter = KalshiAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return {"order_id": "order-1", "remaining_count": "1.00", "fill_count": "0.00"}

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        with patch.dict(
            "os.environ",
            {"KALSHI_API_KEY_ID": "unit-test-key-id", "KALSHI_PRIVATE_KEY_PEM": pem},
        ):
            result = adapter.place_live_order(
                PaperOrderRequest(
                    market_id="kalshi",
                    contract_id="KXFED-26MAY-TARGET-425:YES",
                    side="BUY",
                    size=1,
                    limit_price=0.5,
                    metadata={"client_order_id": "client-1"},
                )
            )

        self.assertEqual(result["response"]["order_id"], "order-1")
        method, url, payload, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/portfolio/events/orders"))
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.5000")
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "unit-test-key-id")
        self.assertTrue(headers["KALSHI-ACCESS-SIGNATURE"])
        self.assertTrue(headers["KALSHI-ACCESS-TIMESTAMP"].isdigit())

    def test_order_management_allows_only_non_increasing_v2_mutations(self) -> None:
        adapter = KalshiAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "kalshi_order_management_enabled": True,
            }
        )
        adapter._auth_headers = lambda method, path: {  # type: ignore[method-assign]
            "KALSHI-ACCESS-KEY": "test-key",
            "KALSHI-ACCESS-SIGNATURE": "test-signature",
            "KALSHI-ACCESS-TIMESTAMP": "123",
        }
        fixtures = {
            "DELETE /portfolio/events/orders/order-kalshi-1": load_fixture("cancel_order_response"),
            "DELETE /portfolio/events/orders/batched": load_fixture("batch_cancel_orders_response"),
            "POST /portfolio/events/orders/order-kalshi-1/decrease": load_fixture("decrease_order_response"),
        }
        calls = []

        class Response:
            status_code = 200
            text = ""

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_request(method, url, *, params=None, json=None, headers=None, timeout=None):
            path = url.split("/trade-api/v2", 1)[-1]
            calls.append((method, path, dict(params or {}), json, dict(headers or {})))
            return Response(fixtures[f"{method} {path}"])

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"

        cancelled = adapter.manage_orders(
            "cancel_order",
            order_id="order-kalshi-1",
            subaccount=0,
            exchange_index=0,
            confirm_order_management=confirmation,
        )
        batch = adapter.manage_orders(
            "batch_cancel_orders",
            orders=[{"order_id": "order-kalshi-1"}, {"order_id": "order-kalshi-2", "subaccount": 1}],
            confirm_order_management=confirmation,
        )
        with self.assertRaisesRegex(MarketConfigurationError, "must be one of"):
            adapter.manage_orders(
                "amend_order",
                order_id="order-kalshi-1",
                ticker="KXFED-26MAY-TARGET-425",
                side="bid",
                price=0.44,
                count=8,
                confirm_order_management=confirmation,
            )
        decreased = adapter.manage_orders(
            "decrease_order",
            order_id="order-kalshi-1",
            reduce_by=5,
            confirm_order_management=confirmation,
        )

        self.assertEqual(cancelled["response"]["order_id"], "order-kalshi-1")
        self.assertEqual(batch["response"]["orders"][1]["order_id"], "order-kalshi-2")
        self.assertEqual(decreased["response"]["remaining_count"], "5.00")
        self.assertEqual(calls[0][0:3], ("DELETE", "/portfolio/events/orders/order-kalshi-1", {"subaccount": 0, "exchange_index": 0}))
        self.assertEqual(calls[1][0:3], ("DELETE", "/portfolio/events/orders/batched", {}))
        self.assertEqual(
            calls[1][3],
            {"orders": [{"order_id": "order-kalshi-1"}, {"order_id": "order-kalshi-2", "subaccount": 1}]},
        )
        self.assertEqual(calls[2][0:3], ("POST", "/portfolio/events/orders/order-kalshi-1/decrease", {}))
        self.assertEqual(calls[2][3], {"reduce_by": "5.00"})

    def test_order_management_requires_opt_in_confirmation_and_strict_shapes(self) -> None:
        disabled = KalshiAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        with self.assertRaises(MarketConfigurationError):
            disabled.manage_orders("cancel_order", order_id="order-1", confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS")

        adapter = KalshiAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "kalshi_order_management_enabled": True,
            }
        )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", order_id="order-1", confirm_order_management="wrong")
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "batch_cancel_orders",
                orders=[{"order_id": "order-1"}] * 51,
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "decrease_order",
                order_id="order-1",
                reduce_by=1,
                reduce_to=0,
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )


if __name__ == "__main__":
    unittest.main()
