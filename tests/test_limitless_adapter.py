from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import LimitlessAdapter, PaperOrderRequest
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "limitless_exchange"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    status_code = 200
    text = '{"orderId":"order-1"}'

    def json(self):
        return {"orderId": "order-1"}


class LimitlessAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None) -> LimitlessAdapter:
        adapter = LimitlessAdapter(config)
        active = load_fixture("active")
        market = load_fixture("market")
        orderbook = load_fixture("orderbook")
        historical_price = load_fixture("historical_price")
        events = load_fixture("events")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/markets/active"):
                return active
            if url.endswith("/markets/doge-above-021652-sep-1-1200-utc"):
                return market
            if url.endswith("/markets/doge-above-021652-sep-1-1200-utc/orderbook"):
                return orderbook
            if url.endswith("/markets/doge-above-021652-sep-1-1200-utc/historical-price"):
                return historical_price
            if url.endswith("/markets/doge-above-021652-sep-1-1200-utc/events"):
                return events
            raise AssertionError(f"unexpected Limitless URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_registered_metadata_advertises_supported_limitless_features(self) -> None:
        adapter = LimitlessAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "limitless_exchange")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertIn("api.limitless.exchange", health["api_base_url"])
        self.assertIn("ws.limitless.exchange", health["websocket_url"])
        self.assertEqual(health["websocket_namespace"], "/markets")
        self.assertEqual(
            health["account_recovery_operations"],
            ["positions", "account_history", "user_orders"],
        )
        self.assertEqual(
            health["order_management_operations"],
            ["cancel_order", "batch_cancel_orders", "cancel_all_orders"],
        )
        self.assertFalse(health["order_management_enabled"])

    def test_list_events_uses_active_market_endpoint_and_filters_query(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("doge", limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].market_id, "limitless_exchange")
        self.assertEqual(events[0].event_id, "doge-above-021652-sep-1-1200-utc")
        self.assertEqual(events[0].status, "active")
        self.assertIn("DOGE", events[0].title)

    def test_list_contracts_creates_yes_and_no_contracts(self) -> None:
        adapter = self.make_adapter()

        contracts = adapter.list_contracts("doge-above-021652-sep-1-1200-utc")

        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].contract_id, "doge-above-021652-sep-1-1200-utc:YES")
        self.assertEqual(contracts[1].contract_id, "doge-above-021652-sep-1-1200-utc:NO")
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertEqual(contracts[1].outcome, "No")

    def test_orderbook_and_price_support_yes_and_no_contracts(self) -> None:
        adapter = self.make_adapter()

        yes_book = adapter.get_orderbook("doge-above-021652-sep-1-1200-utc:YES")
        no_book = adapter.get_orderbook("doge-above-021652-sep-1-1200-utc:NO")
        price = adapter.get_price("doge-above-021652-sep-1-1200-utc:YES")

        self.assertEqual([level.price for level in yes_book.bids], [0.42, 0.4])
        self.assertEqual([level.price for level in yes_book.asks], [0.44, 0.46])
        self.assertEqual([level.price for level in no_book.bids], [0.56, 0.54])
        self.assertEqual([level.price for level in no_book.asks], [0.58, 0.6])
        self.assertEqual(price.bid, 0.42)
        self.assertEqual(price.ask, 0.44)
        self.assertAlmostEqual(price.midpoint or 0, 0.43)

    def test_historical_price_maps_yes_and_no_candles_and_applies_bounds(self) -> None:
        adapter = self.make_adapter()

        yes = adapter.list_candles(
            "doge-above-021652-sep-1-1200-utc:YES",
            resolution="1h",
            from_timestamp=1736943000,
            to_timestamp=1736944300,
        )
        no = adapter.list_candles("doge-above-021652-sep-1-1200-utc:NO", resolution="all")

        self.assertEqual([round(c.close, 2) for c in yes], [0.75, 0.72])
        self.assertEqual([c.timestamp for c in yes], [1736944200.0, 1736943300.0])
        self.assertEqual([round(c.close, 2) for c in no], [0.25, 0.28, 0.3])
        self.assertIsNone(yes[0].volume)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("doge-above-021652-sep-1-1200-utc:YES", resolution="15m")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                "doge-above-021652-sep-1-1200-utc:YES",
                from_timestamp=1736944300,
                to_timestamp=1736943000,
            )

    def test_market_events_maps_finalized_trades_to_yes_or_no_and_scales_size(self) -> None:
        adapter = self.make_adapter()

        yes = adapter.list_trades(
            "doge-above-021652-sep-1-1200-utc:YES",
            limit=10,
            after=1736942000,
            before=1736945100,
        )
        no = adapter.list_trades("doge-above-021652-sep-1-1200-utc:NO", limit=10)

        self.assertEqual([trade.trade_id for trade in yes], ["0xtrade-yes-1", "0xtrade-yes-2"])
        self.assertEqual([trade.side for trade in yes], ["BUY", "BUY"])
        self.assertEqual([trade.size for trade in yes], [1.5, 1.0])
        self.assertEqual([round(trade.price, 2) for trade in yes], [0.75, 0.6])
        self.assertEqual([trade.contract_id for trade in yes], ["doge-above-021652-sep-1-1200-utc:YES"] * 2)
        self.assertEqual(no[0].side, "SELL")
        self.assertEqual(no[0].size, 0.5)
        self.assertEqual(no[0].timestamp, 1736943300.0)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("doge-above-021652-sep-1-1200-utc:YES", limit=0)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(
                "doge-above-021652-sep-1-1200-utc:YES",
                after=1736945100,
                before=1736943000,
            )

    def test_paper_order_builds_delegated_order_shape_without_live_post(self) -> None:
        adapter = self.make_adapter()

        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="limitless_exchange",
                contract_id="doge-above-021652-sep-1-1200-utc:YES",
                side="BUY",
                size=5,
                limit_price=0.43,
                metadata={"order_type": "FAK"},
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertEqual(result.raw["request"]["marketSlug"], "doge-above-021652-sep-1-1200-utc")
        self.assertEqual(result.raw["request"]["orderType"], "FAK")
        self.assertEqual(result.raw["request"]["args"]["tokenId"], "1111111111111111111111111111111111111111111111111111111111111111")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="limitless_exchange",
                    contract_id="doge-above-021652-sep-1-1200-utc:MAYBE",
                    side="BUY",
                    size=5,
                    limit_price=0.43,
                )
            )

    def test_websocket_connection_info_uses_documented_public_market_subscription(self) -> None:
        adapter = self.make_adapter()

        info = adapter.websocket_connection_info(
            market_slugs=["doge-above-021652-sep-1-1200-utc"],
            market_addresses=["0x76d3e2098Be66Aa7E15138F467390f0Eb7349B9b"],
        )

        self.assertEqual(info["url"], "wss://ws.limitless.exchange")
        self.assertEqual(info["namespace"], "/markets")
        self.assertEqual(info["subscribe"]["event"], "subscribe_market_prices")
        self.assertEqual(info["subscribe"]["payload"]["marketSlugs"], ["doge-above-021652-sep-1-1200-utc"])
        self.assertEqual(
            info["subscribe"]["payload"]["marketAddresses"],
            ["0x76d3e2098Be66Aa7E15138F467390f0Eb7349B9b"],
        )

        with self.assertRaises(MarketConfigurationError):
            adapter.websocket_connection_info()

    def test_live_order_is_disabled_by_default(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="limitless_exchange",
                    contract_id="doge-above-021652-sep-1-1200-utc:YES",
                    side="BUY",
                    size=5,
                    limit_price=0.43,
                )
            )

        self.assertIn("disabled", str(ctx.exception))

    def test_live_order_posts_hmac_signed_canonical_json_when_enabled(self) -> None:
        adapter = self.make_adapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            return FakeResponse()

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        secret = base64.b64encode(b"unit-test-secret").decode("ascii")

        with patch.dict(
            "os.environ",
            {
                "LIMITLESS_TOKEN_ID": "token-id",
                "LIMITLESS_TOKEN_SECRET": secret,
                "LIMITLESS_ON_BEHALF_OF": "profile-123",
            },
        ):
            result = adapter.place_live_order(
                PaperOrderRequest(
                    market_id="limitless_exchange",
                    contract_id="doge-above-021652-sep-1-1200-utc:NO",
                    side="SELL",
                    size=2,
                    limit_price=0.55,
                    metadata={"order_type": "GTC", "post_only": True},
                )
            )

        self.assertEqual(result["response"]["orderId"], "order-1")
        method, url, body, headers, timeout = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/orders"))
        self.assertIn('"marketSlug":"doge-above-021652-sep-1-1200-utc"', body)
        self.assertIn('"onBehalfOf":"profile-123"', body)
        self.assertEqual(headers["lmts-api-key"], "token-id")
        self.assertTrue(headers["lmts-signature"])
        self.assertIn("T", headers["lmts-timestamp"])
        self.assertGreater(timeout, 0)

    def test_order_management_posts_fixed_hmac_cancellation_paths(self) -> None:
        adapter = self.make_adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "limitless_order_management_enabled": True,
                "min_request_interval_seconds": 0,
            }
        )
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            return FakeResponse()

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        secret = base64.b64encode(b"unit-test-secret").decode("ascii")
        operator_confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"

        with patch.dict("os.environ", {"LIMITLESS_TOKEN_ID": "token-id", "LIMITLESS_TOKEN_SECRET": secret}):
            single = adapter.manage_orders(
                "cancel_order",
                order_id="order-1",
                confirm_order_management=operator_confirmation,
            )
            batch = adapter.manage_orders(
                "batch_cancel_orders",
                orders=["order-1", "order-2"],
                confirm_order_management=operator_confirmation,
            )
            market = adapter.manage_orders(
                "cancel_all_orders",
                market_slug="doge-above-021652-sep-1-1200-utc",
                confirm_order_management=operator_confirmation,
                confirm_global_cancel="CANCEL ALL LIMITLESS ORDERS",
            )

        self.assertEqual(single["request"], {"orderId": "order-1"})
        self.assertEqual(batch["request"], {"orderIds": ["order-1", "order-2"]})
        self.assertEqual(market["request"], {"marketSlug": "doge-above-021652-sep-1-1200-utc"})
        self.assertEqual([call[0] for call in calls], ["DELETE", "POST", "DELETE"])
        self.assertTrue(calls[0][1].endswith("/orders/order-1"))
        self.assertTrue(calls[1][1].endswith("/orders/cancel-batch"))
        self.assertTrue(calls[2][1].endswith("/orders/all/doge-above-021652-sep-1-1200-utc"))
        self.assertEqual(calls[0][2], "")
        self.assertEqual(calls[2][2], "")
        self.assertEqual(calls[1][2], '{"orderIds":["order-1","order-2"]}')
        self.assertNotIn("Content-Type", calls[0][3])
        self.assertEqual(calls[1][3]["Content-Type"], "application/json")
        self.assertTrue(all(call[3]["lmts-signature"] for call in calls))
        self.assertEqual(single["response"]["orderId"], "order-1")

    def test_order_management_rejects_unsafe_or_unconfirmed_requests_before_http(self) -> None:
        adapter = self.make_adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "limitless_order_management_enabled": True,
            }
        )
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url))
            return FakeResponse()

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        operator_confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"

        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", order_id="../private", confirm_order_management=operator_confirmation)
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "batch_cancel_orders",
                orders=["order-1", "order-1"],
                confirm_order_management=operator_confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_all_orders",
                market_slug="doge-above-021652-sep-1-1200-utc",
                confirm_order_management=operator_confirmation,
                confirm_global_cancel="CANCEL ALL ORDERS",
            )
        self.assertEqual(calls, [])

    def test_authenticated_portfolio_reads_use_hmac_and_delegation_header(self) -> None:
        adapter = self.make_adapter({"limitless_on_behalf_of": "profile-123"})
        positions = load_fixture("positions")
        history = load_fixture("portfolio_history")
        orders = load_fixture("user_orders")
        calls = []

        class AccountResponse:
            status_code = 200
            text = "{}"

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_request(method: str, url: str, *, headers=None, timeout=None):
            calls.append((method, url, headers, timeout))
            if url.endswith("/portfolio/positions"):
                return AccountResponse(positions)
            if url.endswith("/portfolio/history"):
                return AccountResponse(history)
            if url.endswith("/markets/doge-above-021652-sep-1-1200-utc/user-orders"):
                return AccountResponse(orders)
            raise AssertionError(f"unexpected Limitless account URL: {url}")

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        secret = base64.b64encode(b"unit-test-secret").decode("ascii")

        with patch.dict("os.environ", {"LIMITLESS_TOKEN_ID": "token-id", "LIMITLESS_TOKEN_SECRET": secret}):
            self.assertEqual(adapter.account_recovery("positions"), positions)
            self.assertEqual(adapter.account_recovery("account_history"), history)
            self.assertEqual(
                adapter.account_recovery(
                    "user_orders",
                    market_slug="doge-above-021652-sep-1-1200-utc",
                    on_behalf_of="profile-456",
                ),
                orders,
            )
            self.assertEqual(
                adapter.list_user_orders(
                    "doge-above-021652-sep-1-1200-utc",
                    on_behalf_of="profile-456",
                ),
                orders,
            )

        self.assertEqual([call[0] for call in calls], ["GET", "GET", "GET", "GET"])
        self.assertTrue(all(call[2]["lmts-api-key"] == "token-id" for call in calls))
        self.assertTrue(all(call[2]["lmts-signature"] for call in calls))
        self.assertEqual(calls[0][2]["x-on-behalf-of"], "profile-123")
        self.assertEqual(calls[1][2]["x-on-behalf-of"], "profile-123")
        self.assertEqual(calls[2][2]["x-on-behalf-of"], "profile-456")
        self.assertEqual(calls[3][2]["x-on-behalf-of"], "profile-456")
        self.assertTrue(all(call[3] > 0 for call in calls))

        with self.assertRaises(MarketConfigurationError):
            adapter.list_user_orders("../portfolio/positions")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_user_orders("doge/other")

    def test_portfolio_history_supports_simulation_copy_preview(self) -> None:
        adapter = self.make_adapter()
        activity = load_fixture("portfolio_history")["history"][0]

        preview = adapter.copy_trade_from_activity(activity)

        self.assertTrue(preview.accepted)
        self.assertEqual(preview.contract_id, "doge-above-021652-sep-1-1200-utc:YES")
        self.assertEqual(preview.raw["source"], "limitless_portfolio_history")
        self.assertTrue(preview.raw["request"]["dryRun"])

        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**activity, "price": 0})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**activity, "tokenId": "not-the-market-token"})


if __name__ == "__main__":
    unittest.main()
