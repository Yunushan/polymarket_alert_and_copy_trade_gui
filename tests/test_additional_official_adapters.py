from __future__ import annotations

import base64
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import (
    BetfairExchangeAdapter,
    ContextV2Adapter,
    DFlowAdapter,
    GeminiPredictionAdapter,
    HyperliquidAdapter,
    CMEPredictionMarketsAdapter,
    ForecastExAdapter,
    IBKRForecastTraderAdapter,
    IBKREventContractsAdapter,
    MyriadAdapter,
    MatchbookAdapter,
    MetaDAOAdapter,
    OpinionAdapter,
    PaperOrderRequest,
    ProbableAdapter,
    PredictFunAdapter,
    SmarketsAdapter,
    SeerAdapter,
    ThalesMarketAdapter,
    TrueoAdapter,
    XOMarketAdapter,
    XMarketAdapter,
)
from market_adapters.errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(market_id: str, name: str):
    return json.loads((FIXTURES / market_id / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class AdditionalOfficialAdapterTests(unittest.TestCase):
    def test_ibkr_event_contract_adapters_map_forecastex_cme_snapshots_paper_and_guarded_orders(self) -> None:
        forecast_fixtures = {
            name: load_fixture("ibkr_forecasttrader", name)
            for name in (
                "category_tree",
                "search",
                "strikes",
                "info",
                "accounts",
                "snapshot",
                "history",
                "trades",
                "order_response",
                "orders",
                "order_status",
                "cancel_response",
                "modify_response",
            )
        }
        adapter = IBKRForecastTraderAdapter({"ibkr_session_cookie": "api=test-session"})

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {"Cookie": "api=test-session"})
            if url.endswith("/trsrv/event/category-tree"):
                return forecast_fixtures["category_tree"]
            if url.endswith("/iserver/secdef/search"):
                self.assertEqual(params["symbol"], "FF")
                return forecast_fixtures["search"]
            if url.endswith("/iserver/secdef/strikes"):
                self.assertEqual(params["exchange"], "FORECASTX")
                return forecast_fixtures["strikes"]
            if url.endswith("/iserver/secdef/info"):
                self.assertEqual(params["exchange"], "FORECASTX")
                return forecast_fixtures["info"]
            if url.endswith("/iserver/accounts"):
                return forecast_fixtures["accounts"]
            if url.endswith("/iserver/marketdata/snapshot"):
                return forecast_fixtures["snapshot"]
            if url.endswith("/iserver/marketdata/history"):
                self.assertEqual(params["conid"], 721095497)
                self.assertEqual(params["period"], "1h")
                self.assertEqual(params["bar"], "1h")
                self.assertEqual(params["startTime"], "20251009-11:00:00")
                self.assertEqual(params["direction"], -1)
                self.assertEqual(params["source"], "Last")
                self.assertFalse(params["outsideRth"])
                return forecast_fixtures["history"]
            if url.endswith("/iserver/account/trades"):
                self.assertIsNone(params)
                return forecast_fixtures["trades"]
            if url.endswith("/iserver/account/orders"):
                self.assertEqual(params, {"accountId": "DU123456", "filters": "filled", "force": True})
                return forecast_fixtures["orders"]
            if url.endswith("/iserver/account/DU123456/order/status/987654"):
                self.assertIsNone(params)
                return forecast_fixtures["order_status"]
            raise AssertionError(f"unexpected IBKR URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        events = adapter.list_events("FF")
        contracts = adapter.list_contracts(events[0].event_id)
        order = PaperOrderRequest("ibkr_forecasttrader", contracts[0].contract_id, "BUY", 5, 0.48)
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        candles = adapter.list_candles(
            order.contract_id,
            resolution="1h",
            from_timestamp=1760004000,
            to_timestamp=1760007600,
        )
        trades = adapter.list_trades(order.contract_id, limit=2, after=1760010000, before=1760012000)
        paper = adapter.place_paper_order(order)
        copy_preview = adapter.copy_trade_from_activity(forecast_fixtures["trades"][0])

        self.assertEqual(events[0].event_id, "IBKR:FF")
        self.assertEqual({contract.outcome for contract in contracts}, {"YES", "NO"})
        self.assertEqual([level.price for level in book.bids], [0.45])
        self.assertEqual([level.price for level in book.asks], [0.5])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.475)
        self.assertEqual([candle.timestamp for candle in candles], [1760004000.0, 1760007600.0])
        self.assertEqual([candle.close for candle in candles], [0.47, 0.49])
        self.assertEqual([candle.volume for candle in candles], [4.0, 5.0])
        self.assertEqual([trade.trade_id for trade in trades], ["exec-event-buy-1"])
        self.assertEqual([trade.side for trade in trades], ["BUY"])
        self.assertEqual([trade.price for trade in trades], [0.48])
        self.assertEqual([trade.size for trade in trades], [5.0])
        self.assertTrue(paper.accepted)
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, order.contract_id)
        self.assertEqual(copy_preview.average_price, 0.48)
        self.assertEqual(copy_preview.raw["source"], "ibkr_authenticated_account_trades")
        self.assertEqual(copy_preview.raw["execution_id"], "exec-event-buy-1")

        account_adapter = IBKRForecastTraderAdapter(
            {
                "ibkr_session_cookie": "api=test-session",
                "ibkr_account_id": "DU123456",
            }
        )
        account_adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        recovered_orders = account_adapter.account_recovery("orders", filters="filled", force=True)
        recovered_status = account_adapter.account_recovery("order_status", order_id="987654")
        self.assertEqual(recovered_orders["response"]["orders"][0]["orderId"], "987654")
        self.assertEqual(recovered_status["response"]["order_status"], "Submitted")
        self.assertEqual(account_adapter.health_check()["account_recovery_operations"], ["orders", "order_status"])
        self.assertEqual(
            account_adapter.health_check()["order_management_operations"],
            ["cancel_order", "cancel_all_orders", "modify_order"],
        )

        calls = []
        live = IBKRForecastTraderAdapter(
            {
                "ibkr_session_cookie": "api=test-session",
                "ibkr_account_id": "DU123456",
                "ibkr_submit_live_orders": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )

        def fake_live_request(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, params, json_body, headers))
            if method == "GET":
                return forecast_fixtures["accounts"]
            return forecast_fixtures["order_response"]

        live.runtime.request_json = fake_live_request  # type: ignore[method-assign]
        live_result = live.place_live_order(order)
        self.assertTrue(live_result["live"])
        live_call = next(call for call in calls if call[0] == "POST")
        self.assertTrue(live_call[1].endswith("/iserver/account/DU123456/orders"))
        self.assertEqual(live_call[3]["orders"][0]["conid"], 721095497)

        management = IBKRForecastTraderAdapter(
            {
                "ibkr_session_cookie": "api=test-session",
                "ibkr_account_id": "DU123456",
                "ibkr_order_management_enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )
        management_calls = []

        def fake_management_request(method: str, url: str, *, params=None, json_body=None, headers=None):
            management_calls.append((method, url, params, json_body, headers))
            if method == "POST":
                return forecast_fixtures["modify_response"]
            return forecast_fixtures["cancel_response"]

        management.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        management.runtime.request_json = fake_management_request  # type: ignore[method-assign]
        cancelled = management.manage_orders(
            "cancel_order",
            order_id="987654",
            confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        modified = management.manage_orders(
            "modify_order",
            order_id="987654",
            instructions={
                "conid": 721095497,
                "orderType": "LMT",
                "side": "BUY",
                "tif": "DAY",
                "quantity": 5,
                "price": 0.51,
            },
            confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        self.assertEqual(cancelled["response"]["msg"], "Request was submitted")
        self.assertEqual(modified["response"][0]["order_status"], "Submitted")
        self.assertEqual(management_calls[0][0], "DELETE")
        self.assertTrue(management_calls[0][1].endswith("/iserver/account/DU123456/order/987654"))
        self.assertEqual(management_calls[1][0], "POST")
        self.assertEqual(management_calls[1][3]["price"], 0.51)
        with self.assertRaises(MarketConfigurationError):
            management.manage_orders(
                "cancel_all_orders",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                confirm_global_cancel="wrong",
            )
        with self.assertRaises(MarketConfigurationError):
            management.manage_orders(
                "modify_order",
                order_id="../unsafe",
                instructions={},
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

        cme_fixtures = {name: load_fixture("cme_prediction_markets", name) for name in ("search", "info", "accounts", "snapshot")}
        cme = CMEPredictionMarketsAdapter({"ibkr_session_cookie": "api=cme-session", "ibkr_contract_month": "SEP26"})

        def fake_cme_get(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {"Cookie": "api=cme-session"})
            if url.endswith("/iserver/secdef/search"):
                return cme_fixtures["search"]
            if url.endswith("/iserver/secdef/info"):
                return cme_fixtures["info"]
            if url.endswith("/iserver/accounts"):
                return cme_fixtures["accounts"]
            if url.endswith("/iserver/marketdata/snapshot"):
                return cme_fixtures["snapshot"]
            raise AssertionError(f"unexpected CME URL: {url}")

        cme.runtime.get_json = fake_cme_get  # type: ignore[method-assign]
        cme_events = cme.list_events("NQ")
        cme_contracts = cme.list_contracts(cme_events[0].event_id)
        self.assertEqual(cme_events[0].event_id, "IBKR:NQ")
        self.assertEqual({contract.outcome for contract in cme_contracts}, {"YES", "NO"})
        self.assertEqual(len(cme_contracts), 2)
        cme_order = PaperOrderRequest("cme_prediction_markets", cme_contracts[0].contract_id, "SELL", 2, 0.34)
        self.assertTrue(cme.place_paper_order(cme_order).accepted)

        forecastex = ForecastExAdapter({"ibkr_session_cookie": "api=forecastx-session"})
        self.assertIsInstance(forecastex, IBKREventContractsAdapter)
        self.assertEqual(forecastex.metadata.market_id, "forecastex")
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({"side": "BUY"})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**forecast_fixtures["trades"][0], "size": 0})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**forecast_fixtures["trades"][0], "price": 1.2})

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(order.contract_id, resolution="5sec")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(order.contract_id, from_timestamp=1760007600, to_timestamp=1760004000)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(order.contract_id, limit=501)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(order.contract_id, after=1760012000, before=1760009000)

    def test_ibkr_cme_order_management_requires_and_forwards_compliance_fields(self) -> None:
        calls = []
        cme = CMEPredictionMarketsAdapter(
            {
                "ibkr_session_cookie": "api=cme-session",
                "ibkr_account_id": "DU123456",
                "ibkr_order_management_enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "ibkr_api_base_url": "https://localhost:5000/v1/api",
            }
        )

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(url, "https://localhost:5000/v1/api/iserver/accounts")
            self.assertEqual(headers, {"Cookie": "api=cme-session"})
            return {"accounts": ["DU123456"]}

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, params, json_body, headers))
            return [{"order_status": "Submitted"}]

        cme.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        cme.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        result = cme.manage_orders(
            "modify_order",
            order_id="987654",
            instructions={
                "conid": 722021819,
                "orderType": "LMT",
                "side": "SELL",
                "tif": "DAY",
                "quantity": 2,
                "price": 0.34,
            },
            manual_indicator=False,
            external_operator="desk-1",
            confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        self.assertEqual(result["response"][0]["order_status"], "Submitted")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/iserver/account/DU123456/order/987654"))
        self.assertEqual(calls[0][2], {"manualIndicator": False, "extOperator": "desk-1"})
        self.assertEqual(calls[0][3]["side"], "SELL")

        with self.assertRaises(MarketConfigurationError):
            cme.manage_orders(
                "modify_order",
                order_id="987654",
                instructions={
                    "conid": 722021819,
                    "orderType": "LMT",
                    "side": "SELL",
                    "tif": "DAY",
                    "quantity": 2,
                    "price": 0.34,
                },
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

    def test_hyperliquid_public_hip4_fills_support_safe_simulation_copy(self) -> None:
        wallet = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
        adapter = HyperliquidAdapter()
        fills = load_fixture("hyperliquid", "user_fills")

        def fake_request_json(
            method: str,
            url: str,
            *,
            params=None,
            json_body=None,
            headers=None,
        ):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(json_body, {"type": "userFills", "user": wallet, "aggregateByTime": True})
            return fills

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        activities = adapter.list_activity(wallet, limit=10)

        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertEqual(len(activities), 2)
        buy, sell = activities
        self.assertEqual(buy["asset"], "outcome:1:0")
        self.assertEqual(buy["side"], "BUY")
        self.assertAlmostEqual(buy["size"], 5.0)
        self.assertAlmostEqual(buy["price"], 0.63)
        self.assertEqual(buy["timestamp"], 1788264000)
        self.assertEqual(sell["asset"], "outcome:1:1")
        self.assertEqual(sell["side"], "SELL")
        self.assertAlmostEqual(sell["size"], 2.5)
        self.assertAlmostEqual(sell["price"], 0.39)

        copied = adapter.copy_trade_from_activity(sell)
        self.assertTrue(copied.accepted)
        self.assertEqual(copied.contract_id, "outcome:1:1")
        self.assertAlmostEqual(copied.average_price or 0.0, 0.39)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("not-a-wallet")
        with patch.dict("os.environ", {"OPINION_API_KEY": "opinion-key"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_candles("77:YES:0xyes", resolution="30m")
            with self.assertRaises(MarketConfigurationError):
                adapter.list_candles(
                    "77:YES:0xyes",
                    from_timestamp=1733356800,
                    to_timestamp=1733184000,
                )

    def test_hyperliquid_hip4_fills_are_normalized_as_configured_trade_history(self) -> None:
        wallet = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
        fills = load_fixture("hyperliquid", "user_fills")
        adapter = HyperliquidAdapter({"hyperliquid_trade_wallet": wallet})
        requests = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertTrue(url.endswith("/info"))
            requests.append(json_body)
            return fills

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        trades = adapter.list_trades("outcome:1:0", limit=5)
        self.assertTrue(adapter.capabilities.trade_history)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].trade_id, "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(trades[0].side, "BUY")
        self.assertAlmostEqual(trades[0].price, 0.63)
        self.assertAlmostEqual(trades[0].size, 5.0)
        self.assertEqual(trades[0].timestamp, 1788264000)
        self.assertEqual(
            requests[0],
            {"type": "userFills", "user": wallet, "aggregateByTime": True},
        )

        bounded = adapter.list_trades(
            "outcome:1:0",
            limit=5,
            after=1788263999,
            before=1788264000,
        )
        self.assertEqual(len(bounded), 1)
        self.assertEqual(
            requests[1],
            {
                "type": "userFillsByTime",
                "user": wallet,
                "startTime": 1788263999000,
                "endTime": 1788264000000,
                "aggregateByTime": True,
            },
        )

        with self.assertRaises(MarketConfigurationError):
            HyperliquidAdapter().list_trades("outcome:1:0")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("outcome:1:0", limit=1001)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("outcome:1:0", after=1788264001, before=1788264000)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("outcome:1:0", after="not-a-time")

    def test_hyperliquid_account_recovery_reads_use_allowlisted_info_requests(self) -> None:
        wallet = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
        fixtures = {
            name: load_fixture("hyperliquid", name)
            for name in (
                "open_orders",
                "historical_orders",
                "clearinghouse_state",
                "spot_clearinghouse_state",
                "portfolio",
                "subaccounts",
            )
        }
        adapter = HyperliquidAdapter({"hyperliquid_account_wallet": wallet})
        requests = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertTrue(url.endswith("/info"))
            requests.append(dict(json_body))
            return {
                "openOrders": fixtures["open_orders"],
                "historicalOrders": fixtures["historical_orders"],
                "clearinghouseState": fixtures["clearinghouse_state"],
                "spotClearinghouseState": fixtures["spot_clearinghouse_state"],
                "portfolio": fixtures["portfolio"],
                "subAccounts": fixtures["subaccounts"],
            }[str(json_body["type"])]

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        self.assertEqual(adapter.account_recovery_operations[-1], "subaccounts")
        self.assertEqual(adapter.list_active_orders(dex="xyz")[0]["coin"], "#10")
        self.assertEqual(adapter.list_order_history(limit=1)[0]["status"], "filled")
        self.assertEqual(adapter.get_positions(dex="xyz")["withdrawable"], "100.0")
        self.assertEqual(adapter.get_spot_balances()["balances"][0]["coin"], "USDC")
        self.assertEqual(adapter.get_portfolio()[0][0], "day")
        self.assertEqual(adapter.list_subaccounts()[0]["name"], "Trading")
        self.assertEqual(
            requests,
            [
                {"type": "openOrders", "user": wallet, "dex": "xyz"},
                {"type": "historicalOrders", "user": wallet},
                {"type": "clearinghouseState", "user": wallet, "dex": "xyz"},
                {"type": "spotClearinghouseState", "user": wallet},
                {"type": "portfolio", "user": wallet},
                {"type": "subAccounts", "user": wallet},
            ],
        )

        self.assertEqual(adapter.account_recovery("positions", dex="xyz")["withdrawable"], "100.0")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("unsupported")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_active_orders(dex="../private")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_order_history(limit=2001)
        with self.assertRaises(MarketConfigurationError):
            HyperliquidAdapter().get_positions()

    def test_seer_adapter_maps_official_search_prices_and_paper_orders(self) -> None:
        adapter = SeerAdapter()
        markets = load_fixture("seer", "markets_search")
        market = load_fixture("seer", "market")
        market_id = "0x1111111111111111111111111111111111111111"

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(headers, {})
            self.assertIsNone(params)
            if url.endswith("/.netlify/functions/markets-search"):
                self.assertEqual(json_body["marketName"], "Bitcoin")
                return markets
            if url.endswith("/.netlify/functions/get-market"):
                self.assertEqual(json_body, {"chainId": 100, "id": market_id})
                return market
            raise AssertionError(f"unexpected Seer URL: {url}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        event_id = f"100:{market_id}"
        order = PaperOrderRequest("seer", f"100:{market_id}:0", "BUY", 5, 0.6)
        events = adapter.list_events("Bitcoin")
        contracts = adapter.list_contracts(event_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id, event_id)
        self.assertEqual(events[0].status, "active")
        self.assertEqual([contract.outcome for contract in contracts], ["Yes", "No"])
        self.assertAlmostEqual(price.last or 0.0, 0.62)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["outcome_index"], 0)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(order.contract_id)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({"side": "BUY"})

    def test_seer_market_chart_maps_asymmetric_pool_orientation_without_fabricated_sqrt_prices(self) -> None:
        adapter = SeerAdapter()
        market = load_fixture("seer", "market")
        chart = load_fixture("seer", "market_chart")
        market_id = "0x1111111111111111111111111111111111111111"
        chart_requests = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(headers, {})
            if url.endswith("/.netlify/functions/get-market"):
                self.assertEqual(method, "POST")
                self.assertIsNone(params)
                self.assertEqual(json_body, {"chainId": 100, "id": market_id})
                return market
            if url.endswith("/.netlify/functions/market-chart"):
                self.assertEqual(method, "GET")
                self.assertIsNone(json_body)
                self.assertEqual(params, {"marketId": market_id, "chainId": 100})
                chart_requests.append(dict(params))
                return chart
            raise AssertionError(f"unexpected Seer URL: {url}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        yes = adapter.list_candles(
            f"100:{market_id}:0",
            resolution="raw",
            from_timestamp=1733184000,
            to_timestamp=1733187600,
        )
        no = adapter.list_candles(f"100:{market_id}:1", resolution="price")
        all_yes = adapter.list_candles(f"100:{market_id}:0")

        self.assertTrue(adapter.capabilities.candle_history)
        self.assertEqual([candle.timestamp for candle in yes], [1733184000.0, 1733187600.0])
        self.assertEqual([candle.close for candle in yes], [0.61, 0.625])
        self.assertEqual([candle.raw["price_field"] for candle in yes], ["token1Price", "token1Price"])
        self.assertEqual([candle.close for candle in no], [0.41, 0.375])
        self.assertEqual([candle.raw["price_field"] for candle in no], ["token0Price", "token0Price"])
        self.assertEqual(len(all_yes), 3)
        self.assertTrue(all(candle.volume is None for candle in yes + no))
        self.assertEqual(len(chart_requests), 3)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(f"100:{market_id}:0", resolution="1d")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                f"100:{market_id}:0",
                from_timestamp=1733187600,
                to_timestamp=1733184000,
            )

    def test_seer_market_chart_fails_closed_on_unproven_pool_pair_or_missing_dataset(self) -> None:
        market = load_fixture("seer", "market")
        chart = load_fixture("seer", "market_chart")
        market_id = "0x1111111111111111111111111111111111111111"
        bad_pair_chart = json.loads(json.dumps(chart))
        bad_pair_chart[0][0]["pool"]["token1"]["id"] = "0x5555555555555555555555555555555555555555"

        def configured_adapter(payload):
            adapter = SeerAdapter()

            def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
                if url.endswith("/.netlify/functions/get-market"):
                    return market
                if url.endswith("/.netlify/functions/market-chart"):
                    return payload
                raise AssertionError(f"unexpected Seer URL: {url}")

            adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
            return adapter

        with self.assertRaisesRegex(MarketConfigurationError, "pool tokens do not match"):
            configured_adapter(bad_pair_chart).list_candles(f"100:{market_id}:0")
        with self.assertRaisesRegex(MarketConfigurationError, "omitted the dataset"):
            configured_adapter([chart[0]]).list_candles(f"100:{market_id}:1")

        boolean_timestamp_chart = json.loads(json.dumps(chart))
        boolean_timestamp_chart[0][0]["periodStartUnix"] = True
        with self.assertRaisesRegex(MarketConfigurationError, "timestamp"):
            configured_adapter(boolean_timestamp_chart).list_candles(f"100:{market_id}:0")

        boolean_price_chart = json.loads(json.dumps(chart))
        boolean_price_chart[0][0]["token1Price"] = True
        with self.assertRaisesRegex(MarketConfigurationError, "prices"):
            configured_adapter(boolean_price_chart).list_candles(f"100:{market_id}:0")

        with self.assertRaisesRegex(MarketConfigurationError, "from_timestamp"):
            configured_adapter(chart).list_candles(f"100:{market_id}:0", from_timestamp=True)

    def test_seer_guarded_live_order_forwards_reviewed_signed_dex_transaction(self) -> None:
        market_id = "0x1111111111111111111111111111111111111111"
        dex_address = "0x2222222222222222222222222222222222222222"
        tx_hash = "0x" + "ab" * 32
        adapter = SeerAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "seer_submit_signed_transactions": True,
                "seer_rpc_url": "https://rpc.example.invalid/seer",
                "seer_trading_contract_addresses": [dex_address],
            }
        )

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers, {"Content-Type": "application/json"})
            self.assertEqual(url, "https://rpc.example.invalid/seer")
            self.assertEqual(json_body["method"], "eth_sendRawTransaction")
            return {"jsonrpc": "2.0", "id": 1, "result": tx_hash}

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        order = PaperOrderRequest(
            "seer",
            f"100:{market_id}:0",
            "BUY",
            5,
            0.6,
            metadata={
                "signed_transaction": "0x" + "cd" * 96,
                "transaction_to": dex_address,
                "chain_id": "100",
                "market_address": market_id,
                "outcome_index": 0,
                "method": "buy",
                "data": "0x12345678",
            },
        )
        result = adapter.place_live_order(order)
        self.assertTrue(result["live"])
        self.assertEqual(result["tx_hash"], tx_hash)
        self.assertEqual(result["dex_address"], dex_address)
        self.assertEqual(result["chain_id"], "100")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(
                PaperOrderRequest(
                    "seer",
                    order.contract_id,
                    "BUY",
                    5,
                    0.6,
                    metadata={**order.metadata, "transaction_to": "0x3333333333333333333333333333333333333333"},
                )
            )

    def test_hyperliquid_adapter_maps_hip4_outcomes_books_paper_and_signed_orders(self) -> None:
        adapter = HyperliquidAdapter()
        outcome_meta = load_fixture("hyperliquid", "outcome_meta")
        l2_book = load_fixture("hyperliquid", "l2_book")
        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers["Content-Type"], "application/json")
            if url.endswith("/info") and json_body == {"type": "outcomeMeta"}:
                return outcome_meta
            if url.endswith("/info") and json_body == {"type": "l2Book", "coin": "#10"}:
                return l2_book
            if url.endswith("/exchange"):
                return load_fixture("hyperliquid", "exchange_response")
            raise AssertionError(f"unexpected Hyperliquid request: {url} {json_body}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        order = PaperOrderRequest("hyperliquid", "outcome:1:0", "BUY", 5, 0.63)
        events = adapter.list_events("BTC")
        contracts = adapter.list_contracts("outcome:1")
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id, "outcome:1")
        self.assertIn("BTC", events[0].title)
        self.assertEqual([contract.outcome for contract in contracts], ["Yes", "No"])
        self.assertEqual([level.price for level in book.bids], [0.62, 0.6])
        self.assertEqual([level.price for level in book.asks], [0.64, 0.66])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.63)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["action"]["orders"][0]["a"], 100000010)

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = HyperliquidAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        live_adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        signed_action = {
            "action": {
                "type": "order",
                "orders": [
                    {"a": 100000010, "b": True, "p": "0.63", "s": "5", "r": False, "t": {"limit": {"tif": "Gtc"}}}
                ],
                "grouping": "na",
            },
            "nonce": 1788264000000,
            "signature": {"r": "0x1", "s": "0x2", "v": 27},
        }
        result = live_adapter.place_live_order(
            PaperOrderRequest("hyperliquid", "outcome:1:0", "BUY", 5, 0.63, {"signed_action": signed_action})
        )
        self.assertTrue(result["live"])
        self.assertEqual(result["response"]["status"], "ok")

        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({"side": "BUY"})

    def test_hyperliquid_guarded_order_management_validates_signed_exchange_actions(self) -> None:
        adapter = HyperliquidAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "hyperliquid_order_management_enabled": True,
            }
        )
        calls = []
        responses = {
            "cancel": load_fixture("hyperliquid", "cancel_response"),
            "cancelByCloid": load_fixture("hyperliquid", "cancel_response"),
            "modify": load_fixture("hyperliquid", "modify_response"),
            "batchModify": load_fixture("hyperliquid", "modify_response"),
            "scheduleCancel": load_fixture("hyperliquid", "schedule_cancel_response"),
        }

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/exchange"))
            self.assertIsNone(params)
            self.assertEqual(headers, {"Content-Type": "application/json"})
            calls.append(json_body)
            return responses[str(json_body["action"]["type"])]

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        cancel = {
            "action": {"type": "cancel", "cancels": [{"a": 100000010, "o": 123456}]},
            "nonce": 1788264000000,
            "signature": {"r": "0x1", "s": "0x2", "v": 27},
        }
        result = adapter.manage_orders("cancel_order", signed_action=cancel, confirm_order_management=confirmation)
        self.assertEqual(result["response"]["response"]["type"], "cancel")
        self.assertEqual(calls[-1], cancel)

        batch_cancel = {
            "action": {
                "type": "cancel",
                "cancels": [{"a": 100000010, "o": 123456}, {"a": 100000011, "o": 123457}],
            },
            "nonce": 1788264000001,
            "signature": {"r": "0x3", "s": "0x4", "v": 27},
        }
        adapter.manage_orders("cancel_orders", signed_action=batch_cancel, confirm_order_management=confirmation)
        self.assertEqual(calls[-1], batch_cancel)

        by_cloid = {
            "action": {
                "type": "cancelByCloid",
                "cancels": [{"asset": 100000010, "cloid": "0x1234567890abcdef1234567890abcdef"}],
            },
            "nonce": 1788264000002,
            "signature": {"r": "0x5", "s": "0x6", "v": 27},
        }
        adapter.manage_orders("cancel_by_cloid", signed_action=by_cloid, confirm_order_management=confirmation)
        self.assertEqual(calls[-1], by_cloid)

        modify = {
            "action": {
                "type": "modify",
                "oid": 123456,
                "order": {
                    "a": 100000010,
                    "b": True,
                    "p": "0.64",
                    "s": "5",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                },
            },
            "nonce": 1788264000003,
            "signature": {"r": "0x7", "s": "0x8", "v": 27},
        }
        adapter.manage_orders("modify_order", signed_action=modify, confirm_order_management=confirmation)
        self.assertEqual(calls[-1], modify)

        batch_modify = {
            "action": {
                "type": "batchModify",
                "modifies": [
                    {
                        "oid": 123456,
                        "order": {
                            "a": 100000010,
                            "b": True,
                            "p": "0.64",
                            "s": "5",
                            "r": False,
                            "t": {"limit": {"tif": "Gtc"}},
                        },
                    }
                ],
            },
            "nonce": 1788264000004,
            "signature": {"r": "0x9", "s": "0xa", "v": 27},
        }
        adapter.manage_orders("batch_modify_orders", signed_action=batch_modify, confirm_order_management=confirmation)
        self.assertEqual(calls[-1], batch_modify)

        schedule = {
            "action": {"type": "scheduleCancel", "time": int(time.time() * 1000) + 60_000},
            "nonce": 1788264000005,
            "signature": {"r": "0xb", "s": "0xc", "v": 27},
        }
        adapter.manage_orders(
            "schedule_cancel",
            signed_action=schedule,
            confirm_order_management=confirmation,
            confirm_global_cancel="SCHEDULE HYPERLIQUID CANCEL",
        )
        self.assertEqual(calls[-1], schedule)

        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", signed_action=cancel, confirm_order_management="wrong")
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_order",
                signed_action={
                    **cancel,
                    "action": {"type": "cancel", "cancels": [{"a": 100, "o": 123456}]},
                },
                confirm_order_management=confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "schedule_cancel",
                signed_action=schedule,
                confirm_order_management=confirmation,
                confirm_global_cancel="wrong",
            )

    def test_hyperliquid_public_hip4_candles_are_normalized_with_documented_bounds(self) -> None:
        adapter = HyperliquidAdapter()
        candles = load_fixture("hyperliquid", "candles")
        requests = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertIsNone(params)
            self.assertEqual(headers["Content-Type"], "application/json")
            requests.append(json_body)
            self.assertTrue(url.endswith("/info"))
            return candles

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        result = adapter.list_candles(
            "outcome:1:0",
            resolution="1h",
            from_timestamp=1788264000,
            to_timestamp=1788271200,
        )

        self.assertEqual(
            requests,
            [
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": "#10",
                        "interval": "1h",
                        "startTime": 1788264000000,
                        "endTime": 1788271200000,
                    },
                }
            ],
        )
        self.assertEqual([candle.contract_id for candle in result], ["outcome:1:0", "outcome:1:0"])
        self.assertEqual([candle.timestamp for candle in result], [1788264000.0, 1788267600.0])
        self.assertAlmostEqual(result[0].open, 0.62)
        self.assertAlmostEqual(result[0].high, 0.66)
        self.assertAlmostEqual(result[0].low, 0.60)
        self.assertAlmostEqual(result[0].close, 0.64)
        self.assertAlmostEqual(result[0].volume or 0.0, 150.5)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("outcome:1:0", resolution="45m")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                "outcome:1:0",
                from_timestamp=1788271200,
                to_timestamp=1788264000,
            )

    def test_trueo_adapter_maps_onchain_manager_pools_prices_paper_and_signed_tx(self) -> None:
        from eth_abi import encode

        fixture = load_fixture("trueo", "rpc")
        adapter = TrueoAdapter()
        manager = fixture["manager"]
        market = fixture["market"]
        yes_token = fixture["yesToken"]
        no_token = fixture["noToken"]
        payment_token = fixture["paymentToken"]
        yes_pool = fixture["yesPool"]
        no_pool = fixture["noPool"]

        def encoded(types, values):
            return "0x" + encode(types, values).hex()

        def fake_request_json(
            method: str,
            url: str,
            *,
            params=None,
            json_body=None,
            headers=None,
            max_response_bytes=None,
        ):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://mainnet.base.org")
            self.assertEqual(headers, {})
            if isinstance(json_body, list):
                self.assertEqual(max_response_bytes, 2_000_000)
                responses = []
                for request in json_body:
                    response = fake_request_json(
                        method,
                        url,
                        json_body=request,
                        headers=headers,
                        max_response_bytes=max_response_bytes,
                    )
                    response["id"] = request["id"]
                    responses.append(response)
                return responses
            self.assertEqual(json_body["jsonrpc"], "2.0")
            if json_body["method"] == "eth_sendRawTransaction":
                self.assertEqual(json_body["params"], [fixture["signedTransaction"]])
                return {"jsonrpc": "2.0", "id": 1, "result": fixture["transactionHash"]}
            if json_body["method"] == "eth_blockNumber":
                self.assertEqual(json_body["params"], [])
                return {"jsonrpc": "2.0", "id": 1, "result": "0x71"}
            if json_body["method"] == "eth_getLogs":
                self.assertEqual(max_response_bytes, 2_000_000)
                log_filter = json_body["params"][0]
                self.assertEqual(log_filter["address"].lower(), yes_pool.lower())
                self.assertEqual(log_filter["toBlock"], "0x65")
                self.assertEqual(
                    log_filter["topics"],
                    ["0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"],
                )
                return {"jsonrpc": "2.0", "id": 1, "result": fixture["swapLogs"]}
            if json_body["method"] == "eth_getBlockByNumber":
                block_number = json_body["params"][0]
                self.assertFalse(json_body["params"][1])
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"number": block_number, "timestamp": fixture["blockTimestamps"][block_number]},
                }
            self.assertEqual(json_body["method"], "eth_call")
            call = json_body["params"][0]
            target = call["to"].lower()
            data = call["data"]
            selector = data[2:10]
            if target == manager.lower() and selector == "7d6a0d1a":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["uint256"], [1])}
            if target == manager.lower() and selector == "dd5adfa3":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["address"], [market])}
            if target == manager.lower() and selector == "6ec38a4e":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["bool"], [True])}
            if target == market.lower() and selector == "ffa1ad74":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["string"], ["1.2.0"])}
            if target == market.lower() and selector == "066f69af":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["string"], [fixture["question"]])}
            if target == market.lower() and selector == "17447836":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["string"], [fixture["source"]])}
            if target == market.lower() and selector == "4063c865":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["string"], [fixture["additionalInfo"]])}
            if target == market.lower() and selector == "d6a05e67":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["uint256"], [fixture["endOfTrading"]])}
            if target == market.lower() and selector in {"a3dd2619", "2486d671"}:
                value = fixture["status"] if selector == "a3dd2619" else fixture["winningPosition"]
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["uint256"], [value])}
            if target == market.lower() and selector in {"f0d9bb20", "11a9f10a", "3013ce29"}:
                value = {"f0d9bb20": yes_token, "11a9f10a": no_token, "3013ce29": payment_token}[selector]
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["address"], [value])}
            if target == market.lower() and selector == "e4b6db4c":
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["address", "address"], [yes_pool, no_pool])}
            if target == market.lower() and selector in {"b4f2bb6d", "d183feee", "32a3cf96"}:
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}}
            if target in {yes_pool.lower(), no_pool.lower()} and selector in {"0dfe1681", "d21220a7"}:
                value = yes_token if selector == "0dfe1681" else payment_token
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["address"], [value])}
            if target in {yes_pool.lower(), no_pool.lower()} and selector == "3850c7bd":
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": encoded(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], [int(fixture["poolSqrtPriceX96"]), 0, 0, 0, 0, 0, True]),
                }
            if target == yes_token.lower() or target == payment_token.lower():
                return {"jsonrpc": "2.0", "id": 1, "result": encoded(["uint256"], [18])}
            raise AssertionError(f"unexpected Trueo RPC call: {json_body}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        order = PaperOrderRequest("trueo", f"{market}:0", "BUY", 5, 0.5)
        events = adapter.list_events("BTC")
        contracts = adapter.list_contracts(market)
        price = adapter.get_price(order.contract_id)
        trades = adapter.list_trades(order.contract_id, limit=10)
        candles = adapter.list_candles(order.contract_id, resolution="1m")
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id.lower(), market.lower())
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        self.assertAlmostEqual(price.last or 0.0, 1.0)
        self.assertEqual([trade.side for trade in trades], ["SELL", "BUY"])
        self.assertEqual([trade.timestamp for trade in trades], [1700000060.0, 1700000000.0])
        self.assertAlmostEqual(trades[0].price, 0.6)
        self.assertAlmostEqual(trades[0].size, 1.0)
        self.assertAlmostEqual(trades[1].price, 0.5)
        self.assertAlmostEqual(trades[1].size, 2.0)
        self.assertEqual(len(candles), 2)
        self.assertEqual([candle.timestamp for candle in candles], [1699999980.0, 1700000040.0])
        self.assertAlmostEqual(candles[0].close, 0.5)
        self.assertAlmostEqual(candles[0].volume or 0.0, 2.0)
        self.assertTrue(candles[0].raw["partial"])
        self.assertAlmostEqual(candles[1].close, 0.6)
        self.assertAlmostEqual(candles[1].volume or 0.0, 1.0)
        self.assertTrue(candles[1].raw["partial"])
        self.assertTrue(paper.accepted)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(order.contract_id)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(order.contract_id, after=1700000100, before=1700000000)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(order.contract_id, resolution="30m")

        live = TrueoAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "trueo_submit_signed_transactions": True,
                "trueo_chain_id": fixture["transactionChainId"],
                "trueo_live_transaction_targets": [fixture["transactionTo"]],
            }
        )
        live.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        reviewed_metadata = {
            "signed_transaction": fixture["signedTransaction"],
            "chain_id": fixture["transactionChainId"],
            "transaction_to": fixture["transactionTo"],
            "transaction_data": fixture["transactionData"],
            "transaction_value": fixture["transactionValue"],
            "market_address": market,
            "outcome_index": 0,
            "side": "BUY",
            "size": 1,
            "limit_price": 0.5,
        }
        result = live.place_live_order(
            PaperOrderRequest("trueo", f"{market}:0", "BUY", 1, 0.5, reviewed_metadata)
        )
        self.assertTrue(result["live"])
        self.assertEqual(result["tx_hash"], fixture["transactionHash"])
        self.assertEqual(result["chain_id"], fixture["transactionChainId"])
        self.assertEqual(result["transaction_to"].lower(), fixture["transactionTo"].lower())
        self.assertEqual(result["transaction_value"], fixture["transactionValue"])
        self.assertEqual(result["calldata_selector"], fixture["transactionData"])

        rejected_cases = {
            "chain": {**reviewed_metadata, "chain_id": 1},
            "recipient": {**reviewed_metadata, "transaction_to": no_pool},
            "calldata": {**reviewed_metadata, "transaction_data": "0x87654321"},
            "value": {**reviewed_metadata, "transaction_value": 1},
            "market": {**reviewed_metadata, "market_address": no_pool},
            "outcome": {**reviewed_metadata, "outcome_index": 1},
            "side": {**reviewed_metadata, "side": "SELL"},
            "size": {**reviewed_metadata, "size": 2},
            "limit_price": {**reviewed_metadata, "limit_price": 0.6},
        }
        for label, metadata in rejected_cases.items():
            with self.subTest(label=label), self.assertRaises(MarketConfigurationError):
                live.place_live_order(PaperOrderRequest("trueo", f"{market}:0", "BUY", 1, 0.5, metadata))

        without_allowlist = TrueoAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "trueo_submit_signed_transactions": True,
            }
        )
        with self.assertRaises(MarketConfigurationError):
            without_allowlist.place_live_order(
                PaperOrderRequest("trueo", f"{market}:0", "BUY", 1, 0.5, reviewed_metadata)
            )

        with self.assertRaises(MarketConfigurationError):
            live.place_live_order(
                PaperOrderRequest(
                    "trueo",
                    f"{market}:0",
                    "BUY",
                    1,
                    0.5,
                    {**reviewed_metadata, "signed_transaction": "0xdeadbeef"},
                )
            )
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({"side": "BUY"})

    def test_trueo_v4_market_validates_pool_identity_prices_swaps_and_chunking(self) -> None:
        from eth_abi import encode

        fixture = load_fixture("trueo", "rpc_v4")
        manager = fixture["manager"]
        market = fixture["market"]
        pool_manager = fixture["poolManager"]
        state_view = fixture["stateView"]
        yes_token = fixture["yesToken"]
        no_token = fixture["noToken"]
        payment_token = fixture["paymentToken"]
        yes_pool_id = fixture["yesPoolId"]
        no_pool_id = fixture["noPoolId"]
        membership = {"active": True}
        advertised_ids = {"yes": yes_pool_id, "no": no_pool_id}
        history_mode = {"include_old_block": False}
        batch_sizes = []
        log_ranges = []
        block_requests = []

        def encoded(types, values):
            return "0x" + encode(types, values).hex()

        def rpc_response(request):
            request_id = request["id"]

            def success(result):
                return {"jsonrpc": "2.0", "id": request_id, "result": result}

            def reverted():
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": 3, "message": "execution reverted"},
                }

            method = request["method"]
            if method == "eth_getLogs":
                log_filter = request["params"][0]
                self.assertEqual(log_filter["address"].lower(), pool_manager.lower())
                self.assertEqual(
                    log_filter["topics"][0],
                    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
                )
                start = int(log_filter["fromBlock"], 16)
                end = int(log_filter["toBlock"], 16)
                log_ranges.append((start, end))
                pool_id = log_filter["topics"][1]
                logs = fixture["yesSwapLogs"] if pool_id == yes_pool_id else fixture["noSwapLogs"]
                if not start <= 0x65 <= end:
                    logs = []
                elif pool_id == yes_pool_id:
                    if history_mode["include_old_block"]:
                        old_log = {
                            **logs[0],
                            "blockNumber": "0x64",
                            "transactionHash": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        }
                        logs = [old_log, *logs]
                    malformed = {
                        **logs[0],
                        "data": "0x00",
                        "transactionHash": "0xdddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                        "logIndex": "0x3",
                    }
                    logs = [*logs, malformed]
                return success(logs)
            if method == "eth_getBlockByNumber":
                block_number = request["params"][0]
                block_requests.append(block_number)
                return success(
                    {"number": block_number, "timestamp": fixture["blockTimestamps"][block_number]}
                )
            self.assertEqual(method, "eth_call")
            call = request["params"][0]
            target = call["to"].lower()
            data = call["data"]
            selector = data[2:10]
            if target == manager.lower() and selector == "7d6a0d1a":
                return success(encoded(["uint256"], [1]))
            if target == manager.lower() and selector == "dd5adfa3":
                return success(encoded(["address"], [market]))
            if target == manager.lower() and selector == "6ec38a4e":
                return success(encoded(["bool"], [membership["active"]]))
            if target == market.lower() and selector == "ffa1ad74":
                return success(encoded(["string"], ["2.0.0"]))
            if target == market.lower() and selector == "066f69af":
                return success(encoded(["string"], [fixture["question"]]))
            if target == market.lower() and selector == "17447836":
                return success(encoded(["string"], [fixture["source"]]))
            if target == market.lower() and selector == "4063c865":
                return success(encoded(["string"], [fixture["additionalInfo"]]))
            if target == market.lower() and selector == "d6a05e67":
                return success(encoded(["uint256"], [fixture["endOfTrading"]]))
            if target == market.lower() and selector in {"a3dd2619", "2486d671"}:
                value = fixture["status"] if selector == "a3dd2619" else fixture["winningPosition"]
                return success(encoded(["uint256"], [value]))
            if target == market.lower() and selector in {"f0d9bb20", "11a9f10a", "3013ce29"}:
                value = {
                    "f0d9bb20": yes_token,
                    "11a9f10a": no_token,
                    "3013ce29": payment_token,
                }[selector]
                return success(encoded(["address"], [value]))
            if target == market.lower() and selector == "e4b6db4c":
                return reverted()
            if target == market.lower() and selector == "b4f2bb6d":
                return success(
                    encoded(
                        ["bytes32", "bytes32"],
                        [bytes.fromhex(advertised_ids["yes"][2:]), bytes.fromhex(advertised_ids["no"][2:])],
                    )
                )
            if target == market.lower() and selector == "d183feee":
                pool_key = "(address,address,uint24,int24,address)"
                return success(
                    encoded(
                        [pool_key, pool_key],
                        [
                            (yes_token, payment_token, fixture["fee"], fixture["tickSpacing"], fixture["hook"]),
                            (payment_token, no_token, fixture["fee"], fixture["tickSpacing"], fixture["hook"]),
                        ],
                    )
                )
            if target == market.lower() and selector == "32a3cf96":
                return success(encoded(["address"], [fixture["hook"]]))
            if target == state_view.lower() and selector == "dc4c90d3":
                return success(encoded(["address"], [pool_manager]))
            if target == state_view.lower() and selector == "c815641c":
                self.assertIn("0x" + data[10:], {yes_pool_id, no_pool_id})
                return success(
                    encoded(
                        ["uint160", "int24", "uint24", "uint24"],
                        [int(fixture["poolSqrtPriceX96"]), 0, 0, fixture["fee"]],
                    )
                )
            if target in {yes_token.lower(), no_token.lower(), payment_token.lower()} and selector == "313ce567":
                return success(encoded(["uint256"], [18]))
            raise AssertionError(f"unexpected Trueo V4 RPC call: {request}")

        def fake_request_json(
            method: str,
            url: str,
            *,
            params=None,
            json_body=None,
            headers=None,
            max_response_bytes=None,
        ):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://mainnet.base.org")
            self.assertIsNone(params)
            self.assertEqual(headers, {})
            if isinstance(json_body, list):
                batch_sizes.append(len(json_body))
                self.assertLessEqual(len(json_body), 10)
                self.assertEqual(max_response_bytes, 2_000_000)
                return [rpc_response(request) for request in json_body]
            if json_body["method"] == "eth_getLogs":
                self.assertEqual(max_response_bytes, 2_000_000)
            return rpc_response(json_body)

        config = {
            "trueo_log_from_block": 0,
            "trueo_log_to_block": 10_001,
            "trueo_log_window_blocks": 10_000,
            "trueo_log_query_blocks": 10_000,
        }
        adapter = TrueoAdapter(config)
        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        events = adapter.list_events("V4", limit=1)
        contracts = adapter.list_contracts(market)
        yes_price = adapter.get_price(f"{market}:0")
        no_price = adapter.get_price(f"{market}:1")
        yes_trades = adapter.list_trades(f"{market}:0", limit=10)
        no_trades = adapter.list_trades(f"{market}:1", limit=10)
        candles = adapter.list_candles(f"{market}:0", resolution="1m")

        self.assertEqual(len(events), 1)
        self.assertEqual([contract.outcome for contract in contracts], ["YES", "NO"])
        self.assertEqual(contracts[0].raw["market"]["amm_version"], "v4")
        self.assertAlmostEqual(yes_price.last or 0.0, 1.0)
        self.assertAlmostEqual(no_price.last or 0.0, 1.0)
        self.assertEqual([trade.raw["log_index"] for trade in yes_trades], [1, 0])
        self.assertEqual([trade.side for trade in yes_trades], ["BUY", "SELL"])
        self.assertAlmostEqual(yes_trades[0].price, 0.6)
        self.assertEqual([trade.side for trade in no_trades], ["BUY"])
        self.assertAlmostEqual(no_trades[0].price, 0.4)
        self.assertEqual(len(candles), 1)
        self.assertAlmostEqual(candles[0].open, 0.5)
        self.assertAlmostEqual(candles[0].close, 0.6)
        self.assertTrue(batch_sizes)
        self.assertTrue(all(size <= 10 for size in batch_sizes))
        self.assertTrue(log_ranges)
        self.assertTrue(all(end - start + 1 <= 10_000 for start, end in log_ranges))

        history_mode["include_old_block"] = True
        block_requests.clear()
        limited = adapter.list_trades(f"{market}:0", limit=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(block_requests, ["0x65"])

        membership["active"] = False
        unregistered = TrueoAdapter(config)
        unregistered.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with self.assertRaises(MarketConfigurationError):
            unregistered.list_contracts(market)

        membership["active"] = True
        advertised_ids["yes"] = "0x" + "aa" * 32
        mismatched = TrueoAdapter(config)
        mismatched.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with self.assertRaises(MarketConfigurationError):
            mismatched.list_contracts(market)

    def test_trueo_bounded_search_and_history_fail_closed_and_confirm_head(self) -> None:
        adapter = TrueoAdapter({"trueo_event_scan_limit": 1, "trueo_log_window_blocks": 10})
        market = "0x" + "12" * 20
        with (
            patch.object(adapter, "_call_uint", return_value=2),
            patch.object(adapter, "_batch_eth_calls", return_value={"market:1": "0x"}),
            patch.object(adapter, "_call_result_address", return_value=market),
            patch.object(
                adapter,
                "_read_market_summaries",
                return_value={
                    market: {
                        "question": "Unrelated market",
                        "source": "fixture",
                        "status_name": "open",
                    }
                },
            ),
        ):
            with self.assertRaisesRegex(MarketHTTPError, "bounded event scan"):
                adapter.list_events("missing", limit=1)

        with patch.object(adapter, "_rpc", return_value="0x64"):
            self.assertEqual(adapter._trade_log_block_bounds(), (79, 88))

        with patch.object(
            adapter,
            "_batch_block_timestamps",
            return_value={79: 1_000.0, 88: 1_100.0},
        ):
            with self.assertRaisesRegex(MarketConfigurationError, "starts before"):
                adapter._history_coverage(79, 88, after=999.0, before=1_050.0)
            with self.assertRaisesRegex(MarketConfigurationError, "ends after"):
                adapter._history_coverage(79, 88, after=1_000.0, before=1_101.0)
            coverage = adapter._history_coverage(79, 88, after=1_000.0, before=1_100.0)

        self.assertEqual(coverage["confirmation_blocks"], 12)
        self.assertFalse(coverage["reorg_provisional"])

    def test_trueo_batch_retries_top_level_rate_limit_error_within_bound(self) -> None:
        adapter = TrueoAdapter(
            {
                "trueo_rpc_max_retries": 1,
                "trueo_rpc_retry_backoff_seconds": 0,
            }
        )
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "eth_call",
            "params": [{"to": "0x" + "11" * 20, "data": "0x12345678"}, "latest"],
        }
        rate_limit_error = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32016, "message": "over rate limit"},
        }
        payloads = [rate_limit_error, [{"jsonrpc": "2.0", "id": 7, "result": "0x1234"}]]
        attempted_batches = []

        def fake_request_json(method, url, *, json_body=None, headers=None, max_response_bytes=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, adapter.rpc_url)
            self.assertEqual(headers, {})
            self.assertEqual(max_response_bytes, adapter.max_rpc_response_bytes)
            attempted_batches.append(json_body)
            return payloads[len(attempted_batches) - 1]

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        self.assertEqual(adapter._batch_rpc_requests([request]), {7: "0x1234"})
        self.assertEqual(attempted_batches, [[request], [request]])

        exhausted = TrueoAdapter(
            {
                "trueo_rpc_max_retries": 1,
                "trueo_rpc_retry_backoff_seconds": 0,
            }
        )
        exhausted_attempts = []

        def always_rate_limited(method, url, *, json_body=None, headers=None, max_response_bytes=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, exhausted.rpc_url)
            self.assertEqual(headers, {})
            self.assertEqual(max_response_bytes, exhausted.max_rpc_response_bytes)
            exhausted_attempts.append(json_body)
            return rate_limit_error

        exhausted.runtime.request_json = always_rate_limited  # type: ignore[method-assign]

        with self.assertRaisesRegex(MarketHTTPError, "remained rate-limited after bounded retries"):
            exhausted._batch_rpc_requests([request])
        self.assertEqual(exhausted_attempts, [[request], [request]])

    def test_metadao_adapter_maps_official_tickers_prices_and_paper_orders(self) -> None:
        adapter = MetaDAOAdapter()
        tickers = load_fixture("metadao", "tickers")
        ticker_id = tickers[0]["ticker_id"]

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(url, "https://market-api.metadao.fi/api/tickers")
            self.assertEqual(headers, {})
            self.assertIsNone(params)
            return tickers

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("metadao", f"{ticker_id}:0", "BUY", 3, 0.08)

        events = adapter.list_events("META")
        contracts = adapter.list_contracts(ticker_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id, ticker_id)
        self.assertEqual(contracts[0].outcome, "META")
        self.assertAlmostEqual(price.last or 0.0, 0.081340728222)
        self.assertAlmostEqual(price.bid or 0.0, 0.080934024581)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["ticker_id"], ticker_id)

        orderbook = adapter.get_orderbook(order.contract_id)
        self.assertEqual(orderbook.contract_id, order.contract_id)
        self.assertEqual(orderbook.raw["depth"], "top_of_book_only")
        self.assertFalse(orderbook.raw["size_available"])
        self.assertEqual(len(orderbook.bids), 1)
        self.assertEqual(len(orderbook.asks), 1)
        self.assertAlmostEqual(orderbook.bids[0].price, 0.080934024581)
        self.assertAlmostEqual(orderbook.asks[0].price, 0.081747431863)
        self.assertEqual(orderbook.bids[0].size, 0.0)
        self.assertEqual(orderbook.asks[0].size, 0.0)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

    def test_metadao_public_maker_activity_supports_bounded_simulation_copy(self) -> None:
        adapter, contract_id, _requests = self._metadao_history_adapter()
        wallet = "11111111111111111111111111111111"

        activities = adapter.list_activity(wallet, limit=2)

        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertEqual(len(activities), 2)
        self.assertTrue(all(row["proxyWallet"] == f"solana:{wallet}" for row in activities))
        self.assertTrue(all(row["asset"] == contract_id for row in activities))
        self.assertTrue(all(row["side"] in {"BUY", "SELL"} for row in activities))
        self.assertTrue(all(row["transactionHash"] for row in activities))
        self.assertEqual(activities[0]["source"], "metadao_dexscreener_spot_swaps")
        recovered = adapter.account_recovery("activity", wallet=wallet, limit=2)
        self.assertEqual(recovered["source"], "metadao_dexscreener_spot_swaps")
        self.assertEqual(recovered["endpoint"], "/dexscreener/events")
        self.assertEqual(recovered["wallet"], f"solana:{wallet}")
        self.assertEqual(recovered["limit"], 2)
        self.assertEqual(recovered["coverage"], "bounded_recent")
        self.assertEqual(recovered["activity"], activities)

        copied = adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(copied.accepted)
        self.assertEqual(copied.contract_id, contract_id)
        self.assertEqual(copied.raw["request"]["ticker_id"], contract_id.rsplit(":", 1)[0])

        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("not-a-solana-wallet")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("activity", wallet="not-a-solana-wallet")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("positions", wallet=wallet)
        mismatched = dict(activities[0])
        mismatched["maker"] = "22222222222222222222222222222222"
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity(mismatched)

    def _metadao_history_adapter(
        self, *, config=None, latest_block=None, pair=None, events=None, tickers=None
    ):
        ticker_payload = (
            load_fixture("metadao", "tickers") if tickers is None else tickers
        )
        latest_payload = latest_block or load_fixture("metadao", "latest_block")
        pair_payload = pair or load_fixture("metadao", "pair")
        events_payload = events or load_fixture("metadao", "events")
        adapter = MetaDAOAdapter(config or {})
        requests = []

        def clone(payload):
            return json.loads(json.dumps(payload))

        def fake_get_json(url: str, *, params=None, headers=None, max_response_bytes=None):
            self.assertEqual(headers, {})
            request_params = dict(params or {})
            requests.append((url, request_params))
            if url == "https://market-api.metadao.fi/api/tickers":
                self.assertFalse(request_params)
                self.assertIsNone(max_response_bytes)
                return clone(ticker_payload)
            if url == "https://market-api.metadao.fi/dexscreener/latest-block":
                self.assertFalse(request_params)
                self.assertIsNone(max_response_bytes)
                return clone(latest_payload)
            if url == "https://market-api.metadao.fi/dexscreener/pair":
                self.assertEqual(request_params, {"id": ticker_payload[0]["pool_id"]})
                self.assertIsNone(max_response_bytes)
                return clone(pair_payload)
            if url == "https://market-api.metadao.fi/dexscreener/events":
                self.assertEqual(set(request_params), {"fromBlock", "toBlock"})
                expected_cap = int(
                    (config or {}).get("metadao_history_response_byte_cap", 16 * 1024 * 1024)
                )
                self.assertEqual(max_response_bytes, expected_cap)
                if callable(events_payload):
                    return clone(events_payload(request_params))
                return clone(events_payload)
            raise AssertionError(f"unexpected MetaDAO URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        ticker_id = ticker_payload[0]["ticker_id"]
        return adapter, f"{ticker_id}:0", requests

    def test_metadao_swap_history_normalizes_pair_sides_ids_order_and_filters(self) -> None:
        adapter, contract_id, requests = self._metadao_history_adapter()
        fixture_events = load_fixture("metadao", "events")["events"][:6]
        expected_events = sorted(
            fixture_events,
            key=lambda row: (
                row["block"]["blockTimestamp"],
                row["block"]["blockNumber"],
                row["txnIndex"],
                row["eventIndex"],
                row["txnId"],
            ),
            reverse=True,
        )

        trades = adapter.list_trades(contract_id, limit=50)

        self.assertEqual(
            [trade.trade_id for trade in trades],
            [f"{row['txnId']}:{row['txnIndex']}:{row['eventIndex']}" for row in expected_events],
        )
        self.assertEqual([trade.side for trade in trades], ["BUY", "SELL", "BUY", "BUY", "SELL", "BUY"])
        self.assertEqual([trade.size for trade in trades], [100.0, 200.0, 100.0, 250.0, 300.0, 500.0])
        self.assertEqual([trade.price for trade in trades], [0.083, 0.081, 0.085, 0.079, 0.082, 0.08])
        self.assertTrue(all(trade.contract_id == contract_id for trade in trades))
        self.assertNotIn(999.0, [trade.price for trade in trades], "events for a different pair must be ignored")

        filtered = adapter.list_trades(
            contract_id,
            limit=3,
            after=1733040100,
            before=1733043720,
        )
        self.assertEqual([trade.timestamp for trade in filtered], [1733043720.0, 1733043720.0, 1733043660.0])
        self.assertEqual(len(filtered), 3)
        self.assertTrue(all(1733040100 <= float(trade.timestamp or 0) <= 1733043720 for trade in filtered))
        self.assertGreaterEqual(
            sum(url.endswith("/dexscreener/events") for url, _params in requests),
            2,
        )

    def test_metadao_swap_history_derives_chronological_auditable_ohlcv(self) -> None:
        adapter, contract_id, _requests = self._metadao_history_adapter()
        fixture_events = load_fixture("metadao", "events")["events"]
        trade_ids = [f"{row['txnId']}:{row['txnIndex']}:{row['eventIndex']}" for row in fixture_events]

        candles = adapter.list_candles(
            contract_id,
            resolution="1h",
            from_timestamp=1733040000,
            to_timestamp=1733047199,
        )

        self.assertEqual([candle.timestamp for candle in candles], [1733040000.0, 1733043600.0])
        self.assertEqual(
            [(candle.open, candle.high, candle.low, candle.close, candle.volume) for candle in candles],
            [
                (0.08, 0.082, 0.079, 0.079, 1050.0),
                (0.085, 0.085, 0.081, 0.083, 400.0),
            ],
        )
        self.assertTrue(all(candle.raw.get("derived") is True for candle in candles))
        self.assertTrue(all(candle.raw.get("partial") is False for candle in candles))
        self.assertTrue(
            all(candle.raw.get("history_coverage") == "complete_pair_scan" for candle in candles)
        )
        self.assertEqual(
            candles[0].raw.get("trade_ids"),
            trade_ids[:3],
        )
        self.assertEqual(
            candles[1].raw.get("trade_ids"),
            trade_ids[3:6],
        )

        fractional_start = adapter.list_candles(
            contract_id,
            resolution="1h",
            from_timestamp=1733040000.5,
            to_timestamp=1733047199,
        )
        self.assertEqual(
            [candle.timestamp for candle in fractional_start],
            [1733043600.0],
            "a fractional in-bucket start must advance to the next complete bucket",
        )

    def test_metadao_swap_history_uses_slot_order_when_block_timestamps_are_not_monotonic(self) -> None:
        rows = load_fixture("metadao", "events")["events"][:2]
        rows[1]["block"]["blockTimestamp"] = 1733040000
        adapter, contract_id, _requests = self._metadao_history_adapter(events={"events": rows})

        trades = adapter.list_trades(contract_id)
        self.assertEqual(
            [trade.raw["block_number"] for trade in trades],
            [312000020, 312000010],
        )
        candles = adapter.list_candles(
            contract_id,
            resolution="1h",
            from_timestamp=1733040000,
            to_timestamp=1733043599,
        )
        self.assertEqual(len(candles), 1)
        self.assertEqual((candles[0].open, candles[0].close), (0.08, 0.082))

        latest = {"block": {"blockNumber": 130, "blockTimestamp": 1300}}
        pair = load_fixture("metadao", "pair")
        pair["pair"]["createdAtBlockNumber"] = 100
        pair["pair"]["createdAtBlockTimestamp"] = 1000
        bounded_rows = load_fixture("metadao", "events")["events"][1:3]
        bounded_rows[0]["block"] = {"blockNumber": 120, "blockTimestamp": 1200}
        bounded_rows[1]["block"] = {"blockNumber": 130, "blockTimestamp": 1300}
        bounded, bounded_contract, _requests = self._metadao_history_adapter(
            config={"metadao_history_slot_window": 10, "metadao_history_max_windows": 1},
            latest_block=latest,
            pair=pair,
            events={"events": bounded_rows},
        )

        partial_candles = bounded.list_candles(bounded_contract, resolution="1m")

        self.assertEqual([candle.timestamp for candle in partial_candles], [1260.0])
        self.assertTrue(partial_candles[0].raw["partial"])
        self.assertEqual(
            partial_candles[0].raw["history_coverage"], "bounded_slot_slice"
        )

    def test_metadao_swap_history_rejects_pair_identity_mismatches(self) -> None:
        base_pair = load_fixture("metadao", "pair")
        cases = {
            "pair id": ("id", "11111111111111111111111111111111"),
            "base mint": ("asset0Id", "So11111111111111111111111111111111111111112"),
            "quote mint": ("asset1Id", "So11111111111111111111111111111111111111112"),
        }
        for name, (field, value) in cases.items():
            with self.subTest(name=name):
                pair = json.loads(json.dumps(base_pair))
                pair["pair"][field] = value
                adapter, contract_id, _requests = self._metadao_history_adapter(pair=pair)
                with self.assertRaises(MarketConfigurationError):
                    adapter.list_trades(contract_id)

    def test_metadao_swap_history_rejects_ambiguous_ticker_pool_identity(self) -> None:
        tickers = load_fixture("metadao", "tickers")
        duplicate = json.loads(json.dumps(tickers[0]))
        duplicate["pool_id"] = "So11111111111111111111111111111111111111112"
        adapter, contract_id, _requests = self._metadao_history_adapter(
            tickers=[tickers[0], duplicate]
        )

        with self.assertRaisesRegex(MarketConfigurationError, "ambiguous"):
            adapter.list_trades(contract_id)

    def test_metadao_swap_history_does_not_use_dex_label_as_pair_identity(self) -> None:
        pair = load_fixture("metadao", "pair")
        pair["pair"]["dexKey"] = "futarchy"
        adapter, contract_id, _requests = self._metadao_history_adapter(pair=pair)

        trades = adapter.list_trades(contract_id)

        self.assertEqual(len(trades), 6)
        self.assertTrue(all(trade.raw["pair"]["dexKey"] == "futarchy" for trade in trades))

    def test_metadao_swap_history_rejects_malformed_ambiguous_and_inconsistent_events(self) -> None:
        template = load_fixture("metadao", "events")["events"][0]
        cases = {}

        event = json.loads(json.dumps(template))
        event["eventType"] = "transfer"
        cases["wrong event type"] = event
        event = json.loads(json.dumps(template))
        event["priceNative"] = True
        cases["boolean price"] = event
        event = json.loads(json.dumps(template))
        event["priceNative"] = "NaN"
        cases["non-finite price"] = event
        event = json.loads(json.dumps(template))
        event["asset1In"] = True
        cases["boolean amount"] = event
        event = json.loads(json.dumps(template))
        event["asset0In"] = 10.0
        event["asset1Out"] = 0.8
        cases["ambiguous direction"] = event
        event = json.loads(json.dumps(template))
        event["priceNative"] = 0.5
        cases["price disagrees with amounts"] = event
        event = json.loads(json.dumps(template))
        event["txnId"] = ""
        cases["missing transaction id"] = event
        event = json.loads(json.dumps(template))
        event["txnId"] = "2" * 64
        cases["transaction id does not decode to a Solana signature"] = event
        event = json.loads(json.dumps(template))
        event["maker"] = "not-a-solana-address"
        cases["invalid maker"] = event
        event = json.loads(json.dumps(template))
        event["txnIndex"] = True
        cases["boolean transaction index"] = event
        event = json.loads(json.dumps(template))
        event["block"]["blockTimestamp"] = "Infinity"
        cases["non-finite timestamp"] = event
        event = json.loads(json.dumps(template))
        event["asset0Out"] = 0
        cases["zero size"] = event
        event = json.loads(json.dumps(template))
        event["asset1In"] = 10**400
        cases["overflowing amount"] = event
        event = json.loads(json.dumps(template))
        event["block"]["blockTimestamp"] = 0
        cases["zero block timestamp"] = event

        for name, bad_event in cases.items():
            with self.subTest(name=name):
                adapter, contract_id, _requests = self._metadao_history_adapter(events={"events": [bad_event]})
                with self.assertRaises(MarketConfigurationError):
                    adapter.list_trades(contract_id)

    def test_metadao_swap_history_rejects_duplicate_trade_identity(self) -> None:
        event = load_fixture("metadao", "events")["events"][0]
        conflicting = json.loads(json.dumps(event))
        conflicting["maker"] = "So11111111111111111111111111111111111111112"
        adapter, contract_id, _requests = self._metadao_history_adapter(
            events={"events": [event, conflicting]}
        )

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(contract_id)

    def test_metadao_swap_history_scans_bounded_disjoint_inclusive_windows(self) -> None:
        latest = {"block": {"blockNumber": 130, "blockTimestamp": 1300}}
        pair = load_fixture("metadao", "pair")
        pair["pair"]["createdAtBlockNumber"] = 100
        pair["pair"]["createdAtBlockTimestamp"] = 1000
        templates = load_fixture("metadao", "events")["events"][:2]
        event_requests = []

        def events_for_range(params):
            start = int(params["fromBlock"])
            end = int(params["toBlock"])
            event_requests.append((start, end))
            rows = []
            if start <= 100 <= end:
                creation = json.loads(json.dumps(templates[0]))
                creation["block"] = {"blockNumber": 100, "blockTimestamp": 1000}
                rows.append(creation)
            if start <= 101 <= end:
                desired = json.loads(json.dumps(templates[1]))
                desired["block"] = {"blockNumber": 101, "blockTimestamp": 1010}
                rows.append(desired)
            return {"events": rows}

        adapter, contract_id, _requests = self._metadao_history_adapter(
            config={
                "metadao_history_slot_window": 10,
                "metadao_history_max_windows": 3,
                "metadao_history_event_cap": 100,
            },
            latest_block=latest,
            pair=pair,
            events=events_for_range,
        )

        trades = adapter.list_trades(contract_id, after=1001)

        self.assertEqual(len(trades), 1)
        self.assertEqual(len(event_requests), 3)
        for start, end in event_requests:
            self.assertGreaterEqual(start, 100)
            self.assertLessEqual(start, end)
            self.assertLessEqual(end - start, 10)
        for previous, current in zip(event_requests, event_requests[1:], strict=False):
            self.assertLess(current[1], previous[0], "inclusive scan windows must not overlap")

    def test_metadao_timestamp_coverage_reaches_creation_across_nonmonotonic_windows(self) -> None:
        latest = {"block": {"blockNumber": 130, "blockTimestamp": 1300}}
        pair = load_fixture("metadao", "pair")
        pair["pair"]["createdAtBlockNumber"] = 100
        pair["pair"]["createdAtBlockTimestamp"] = 1000
        templates = load_fixture("metadao", "events")["events"][:4]
        event_requests = []

        def events_for_range(params):
            start = int(params["fromBlock"])
            end = int(params["toBlock"])
            event_requests.append((start, end))
            if start == 120:
                event = json.loads(json.dumps(templates[1]))
                event["block"] = {"blockNumber": 120, "blockTimestamp": 1190}
                return {"events": [event]}
            if start == 109:
                newer_timestamp = json.loads(json.dumps(templates[2]))
                newer_timestamp["block"] = {"blockNumber": 119, "blockTimestamp": 1210}
                equal_boundary = json.loads(json.dumps(templates[3]))
                equal_boundary["block"] = {"blockNumber": 118, "blockTimestamp": 1200}
                return {"events": [equal_boundary, newer_timestamp]}
            if start == 100:
                creation = json.loads(json.dumps(templates[0]))
                creation["block"] = {"blockNumber": 100, "blockTimestamp": 1000}
                return {"events": [creation]}
            return {"events": []}

        adapter, contract_id, _requests = self._metadao_history_adapter(
            config={"metadao_history_slot_window": 10, "metadao_history_max_windows": 3},
            latest_block=latest,
            pair=pair,
            events=events_for_range,
        )

        trades = adapter.list_trades(contract_id, after=1200)

        self.assertEqual([trade.timestamp for trade in trades], [1210.0, 1200.0])
        self.assertEqual(event_requests, [(120, 130), (109, 119), (100, 108)])

        missing, missing_contract, _requests = self._metadao_history_adapter(
            config={"metadao_history_slot_window": 10, "metadao_history_max_windows": 3},
            latest_block=latest,
            pair=pair,
            events=lambda _params: {"events": []},
        )
        with self.assertRaisesRegex(MarketConfigurationError, "declared first swap"):
            missing.list_trades(missing_contract, after=1200)

    def test_metadao_swap_history_fails_when_requested_range_is_not_covered(self) -> None:
        latest = {"block": {"blockNumber": 130, "blockTimestamp": 1300}}
        pair = load_fixture("metadao", "pair")
        pair["pair"]["createdAtBlockNumber"] = 100
        pair["pair"]["createdAtBlockTimestamp"] = 1000
        template = load_fixture("metadao", "events")["events"][0]

        def events_for_range(params):
            end = int(params["toBlock"])
            block_number = end
            event = json.loads(json.dumps(template))
            event["block"] = {"blockNumber": block_number, "blockTimestamp": block_number * 10}
            base58_alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            event["txnId"] = "1" * 63 + base58_alphabet[block_number % len(base58_alphabet)]
            return {"events": [event]}

        adapter, contract_id, _requests = self._metadao_history_adapter(
            config={"metadao_history_slot_window": 10, "metadao_history_max_windows": 2},
            latest_block=latest,
            pair=pair,
            events=events_for_range,
        )

        with self.assertRaisesRegex(MarketConfigurationError, "(?i)(cover|range|window)"):
            adapter.list_trades(contract_id, after=1000)

    def test_metadao_swap_history_enforces_event_cap_and_config_bounds(self) -> None:
        events = load_fixture("metadao", "events")
        adapter, contract_id, _requests = self._metadao_history_adapter(
            config={"metadao_history_event_cap": 3},
            events=events,
        )
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(contract_id)

        invalid_configs = (
            {"metadao_history_slot_window": 0},
            {"metadao_history_slot_window": 500001},
            {"metadao_history_slot_window": True},
            {"metadao_history_max_windows": 0},
            {"metadao_history_max_windows": 51},
            {"metadao_history_max_windows": True},
            {"metadao_history_event_cap": 0},
            {"metadao_history_event_cap": 250001},
            {"metadao_history_event_cap": True},
            {"metadao_history_max_windows": float("inf")},
            {"metadao_history_response_byte_cap": 1023},
            {"metadao_history_response_byte_cap": 67108865},
            {"metadao_history_response_byte_cap": True},
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                invalid, invalid_contract, _requests = self._metadao_history_adapter(config=config)
                with self.assertRaises(MarketConfigurationError):
                    invalid.list_trades(invalid_contract)

        adapter, contract_id, _requests = self._metadao_history_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(contract_id, limit=float("inf"))
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(contract_id, after=10**10000)

    def test_metadao_empty_candle_range_still_validates_contract_config_and_identity(self) -> None:
        adapter, _contract_id, _requests = self._metadao_history_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(
                "invalid",
                resolution="1h",
                from_timestamp=1733040000.5,
                to_timestamp=1733040001,
            )

        invalid, invalid_contract, _requests = self._metadao_history_adapter(
            config={"metadao_history_response_byte_cap": 1}
        )
        with self.assertRaises(MarketConfigurationError):
            invalid.list_candles(
                invalid_contract,
                resolution="1h",
                from_timestamp=1733040000.5,
                to_timestamp=1733040001,
            )

        tickers = load_fixture("metadao", "tickers")
        duplicate = json.loads(json.dumps(tickers[0]))
        duplicate["pool_id"] = "So11111111111111111111111111111111111111112"
        ambiguous, ambiguous_contract, _requests = self._metadao_history_adapter(
            tickers=[tickers[0], duplicate]
        )
        with self.assertRaisesRegex(MarketConfigurationError, "ambiguous"):
            ambiguous.list_candles(
                ambiguous_contract,
                resolution="1h",
                from_timestamp=1733040000.5,
                to_timestamp=1733040001,
            )

    def test_metadao_swap_history_rejects_events_outside_requested_slot_window(self) -> None:
        latest = {"block": {"blockNumber": 130, "blockTimestamp": 1300}}
        pair = load_fixture("metadao", "pair")
        pair["pair"]["createdAtBlockNumber"] = 100
        pair["pair"]["createdAtBlockTimestamp"] = 1000
        event = load_fixture("metadao", "events")["events"][0]
        event["block"] = {"blockNumber": 100, "blockTimestamp": 1000}
        adapter, contract_id, _requests = self._metadao_history_adapter(
            config={"metadao_history_slot_window": 10, "metadao_history_max_windows": 1},
            latest_block=latest,
            pair=pair,
            events={"events": [event]},
        )

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(contract_id)

    def test_metadao_guarded_live_order_forwards_reviewed_signed_router_transaction(self) -> None:
        tickers = load_fixture("metadao", "tickers")
        row = tickers[0]
        ticker_id = row["ticker_id"]
        router = "11111111111111111111111111111111"
        signature = "1" * 64
        adapter = MetaDAOAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "metadao_submit_signed_transactions": True,
                "metadao_solana_rpc_url": "https://rpc.example.invalid/metadao",
                "metadao_router_program_ids": [router],
            }
        )

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(url, "https://market-api.metadao.fi/api/tickers")
            self.assertEqual(headers, {})
            self.assertIsNone(params)
            return tickers

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://rpc.example.invalid/metadao")
            self.assertIsNone(params)
            self.assertEqual(headers, {"Content-Type": "application/json"})
            self.assertEqual(json_body["method"], "sendTransaction")
            return {"jsonrpc": "2.0", "id": 1, "result": signature}

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        order = PaperOrderRequest(
            "metadao",
            f"{ticker_id}:0",
            "BUY",
            3,
            0.08,
            {
                "signed_transaction": base64.b64encode(b"\x01" * 96).decode("ascii"),
                "router_program_id": router,
                "ticker_id": ticker_id,
                "pool_id": row["pool_id"],
                "instruction": "swap",
                "instruction_data": "AQIDBA==",
            },
        )
        result = adapter.place_live_order(order)
        self.assertTrue(result["live"])
        self.assertEqual(result["signature"], signature)
        self.assertEqual(result["ticker_id"], ticker_id)
        self.assertEqual(result["router_program_id"], router)

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(
                PaperOrderRequest(
                    "metadao",
                    order.contract_id,
                    "BUY",
                    3,
                    0.08,
                    {**order.metadata, "router_program_id": "22222222222222222222222222222222"},
                )
            )

    def test_thales_adapter_maps_amm_markets_prices_paper_orders_and_safety_gates(self) -> None:
        adapter = ThalesMarketAdapter()
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertFalse(adapter.health_check()["live_trading_enabled"])
        self.assertTrue(adapter.health_check()["wallet_transaction_required"])
        markets = load_fixture("thales_market", "markets")
        market = load_fixture("thales_market", "market")
        quote = load_fixture("thales_market", "buy_quote")
        address = "0x1111111111111111111111111111111111111111"

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {})
            self.assertIn("/thales/networks/10/", url)
            if url.endswith("/markets"):
                return markets
            if url.endswith(f"/markets/{address}"):
                return market
            if url.endswith(f"/markets/{address}/buy-quote"):
                return quote
            raise AssertionError(f"unexpected Thales URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("thales_market", f"{address}:0", "BUY", 5, 0.57)

        events = adapter.list_events("BTC")
        contracts = adapter.list_contracts(address)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events[0].event_id, address)
        self.assertEqual([contract.outcome for contract in contracts], ["UP", "DOWN"])
        self.assertAlmostEqual(price.last or 0.0, 0.58)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["network"], "10")
        self.assertEqual(paper.raw["request"]["position"], "UP")

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(order.contract_id)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)
        with self.assertRaises(UnsupportedFeatureError):
            adapter.copy_trade_from_activity({"side": "BUY"})

        amm_address = "0x2222222222222222222222222222222222222222"
        signed = "0x" + ("11" * 32)
        tx_hash = "0x" + ("aa" * 32)
        live_adapter = ThalesMarketAdapter(
            {
                "thales_network": "10",
                "thales_rpc_url": "https://rpc.example",
                "thales_amm_address": amm_address,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "thales_submit_signed_transactions": True,
            }
        )
        rpc_calls = []

        def fake_request_json(method: str, url: str, *, json_body=None, headers=None, params=None):
            rpc_calls.append((method, url, json_body, headers, params))
            return {"jsonrpc": "2.0", "id": 1, "result": tx_hash}

        live_adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        live = live_adapter.place_live_order(
            PaperOrderRequest(
                "thales_market",
                order.contract_id,
                "BUY",
                5,
                0.57,
                {
                    "signed_transaction": signed,
                    "transaction_to": amm_address,
                    "chain_id": 10,
                    "method": "buyFromAmm",
                    "data": "0x12345678" + ("00" * 32),
                    "market_address": address,
                    "position": "UP",
                },
            )
        )
        self.assertTrue(live["live"])
        self.assertEqual(live["tx_hash"], tx_hash)
        self.assertEqual(live["method"], "buyFromAmm")
        self.assertEqual(rpc_calls[0][0], "POST")
        self.assertEqual(rpc_calls[0][2]["method"], "eth_sendRawTransaction")
        self.assertEqual(rpc_calls[0][2]["params"], [signed])

        with self.assertRaises(MarketConfigurationError):
            live_adapter.place_live_order(
                PaperOrderRequest(
                    "thales_market",
                    order.contract_id,
                    "BUY",
                    5,
                    0.57,
                    {
                        "signed_transaction": signed,
                        "transaction_to": "0x3333333333333333333333333333333333333333",
                        "chain_id": 10,
                        "method": "buyFromAmm",
                        "data": "0x12345678",
                    },
                )
            )

    def test_thales_account_subgraph_reads_positions_and_transactions(self) -> None:
        wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        market = "0x1111111111111111111111111111111111111111"
        positions = load_fixture("thales_market", "account_positions")
        transactions = load_fixture("thales_market", "account_transactions")
        adapter = ThalesMarketAdapter(
            {
                "thales_network": "10",
                "thales_subgraph_url": "https://graph.example/thales",
            }
        )
        calls = []

        def fake_request_json(method: str, url: str, *, json_body=None, headers=None, params=None):
            calls.append((method, url, json_body, headers, params))
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://graph.example/thales")
            self.assertEqual(headers, {"Content-Type": "application/json"})
            query = str(json_body["query"])
            if "ThalesPositions" in query:
                self.assertEqual(json_body["variables"], {"account": wallet, "first": 25})
                return positions
            if "ThalesTransactions" in query:
                self.assertEqual(
                    json_body["variables"],
                    {
                        "account": wallet,
                        "first": 10,
                        "market": market,
                        "from": "1780000000",
                        "to": "1790000000",
                    },
                )
                return transactions
            raise AssertionError("unexpected Thales GraphQL query")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        recovered_positions = adapter.account_recovery("positions", wallet=wallet, limit=25)
        recovered_transactions = adapter.account_recovery(
            "transactions",
            wallet=wallet,
            limit=10,
            market_id=market,
            from_timestamp=1780000000,
            to_timestamp=1790000000,
        )

        self.assertEqual(adapter.health_check()["account_recovery_operations"], ["positions", "transactions"])
        self.assertEqual(recovered_positions["account"], wallet)
        self.assertEqual(recovered_positions["positions"][0]["id"], "position-balance-1")
        self.assertEqual(recovered_positions["ranged_positions"], [])
        self.assertEqual(recovered_transactions["transactions"][0]["market"], market)
        self.assertEqual(len(calls), 2)

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("positions", wallet="not-an-address")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("transactions", wallet=wallet, from_timestamp=2, to_timestamp=1)
        with self.assertRaises(MarketConfigurationError):
            ThalesMarketAdapter().account_recovery("positions", wallet=wallet)

    def test_smarkets_adapter_maps_events_contracts_quotes_paper_and_guarded_orders(self) -> None:
        adapter = SmarketsAdapter()
        events = load_fixture("smarkets", "events")
        markets = load_fixture("smarkets", "markets")
        contracts = load_fixture("smarkets", "contracts")
        quotes = load_fixture("smarkets", "quotes")
        orders = load_fixture("smarkets", "orders")
        account = load_fixture("smarkets", "account")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {"Authorization": "Session-Token smk-token"})
            if url.endswith("/events/"):
                return events
            if url.endswith("/events/event-1/markets/"):
                return markets
            if url.endswith("/markets/market-1/contracts/"):
                return contracts
            if url.endswith("/markets/market-1/quotes/"):
                return quotes
            if url.endswith("/orders/"):
                return orders
            if url.endswith("/accounts/"):
                return account
            raise AssertionError(f"unexpected Smarkets URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("smarkets", "market-1:contract-yes", "BUY", 5, 0.44)
        with patch.dict("os.environ", {"SMARKETS_SESSION_TOKEN": "smk-token"}):
            events_result = adapter.list_events("Bitcoin")
            contract_rows = adapter.list_contracts("event-1")
            book = adapter.get_orderbook(order.contract_id)
            price = adapter.get_price(order.contract_id)
            paper = adapter.place_paper_order(order)
            recovered_orders = adapter.account_recovery("order_history", status="created", limit=25)
            recovered_account = adapter.account_recovery("account")
            trades = adapter.list_trades(order.contract_id, limit=10)
            candles = adapter.list_candles(order.contract_id, resolution="1h")

        self.assertEqual(events_result[0].event_id, "event-1")
        self.assertEqual([contract.contract_id for contract in contract_rows], ["market-1:contract-yes", "market-1:contract-no"])
        self.assertEqual([level.price for level in book.bids], [0.42, 0.4])
        self.assertEqual([level.price for level in book.asks], [0.46, 0.48])
        self.assertAlmostEqual(price.last or 0.0, 0.43)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["price"], "4400")
        self.assertEqual(recovered_orders["orders"][0]["id"], "order-1")
        self.assertEqual(recovered_account["accounts"][0]["currency"], "GBP")
        self.assertEqual([trade.trade_id for trade in trades], ["order-filled-1"])
        self.assertEqual(trades[0].side, "BUY")
        self.assertAlmostEqual(trades[0].price, 0.45)
        self.assertAlmostEqual(trades[0].size, 2.5)
        self.assertAlmostEqual(trades[0].timestamp or 0.0, 1787382060.0)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].contract_id, order.contract_id)
        self.assertAlmostEqual(candles[0].open, 0.45)
        self.assertAlmostEqual(candles[0].close, 0.45)
        self.assertAlmostEqual(candles[0].volume or 0.0, 2.5)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(adapter.health_check()["account_recovery_operations"], ["order_history", "account"])
        self.assertEqual(adapter.health_check()["order_management_operations"], ["cancel_order", "cancel_orders"])
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = SmarketsAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append((method, url, json, headers, timeout))
            return FakeResponse(load_fixture("smarkets", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"SMARKETS_SESSION_TOKEN": "smk-token"}):
            result = live_adapter.place_live_order(order)
        self.assertTrue(result["live"])
        self.assertEqual(result["response"]["orders"][0]["id"], "order-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders/"))
        self.assertEqual(calls[0][2]["side"], "buy")
        self.assertEqual(calls[0][3]["Authorization"], "Session-Token smk-token")

        management_adapter = SmarketsAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "smarkets_order_management_enabled": True,
            }
        )
        management_calls = []

        def fake_management_request(method: str, url: str, *, json=None, headers=None, timeout=None, params=None):
            management_calls.append((method, url, json, headers, timeout, params))
            return FakeResponse(load_fixture("smarkets", "cancel_response"))

        management_adapter.runtime.session.request = fake_management_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"SMARKETS_SESSION_TOKEN": "smk-token"}):
            cancelled = management_adapter.manage_orders(
                "cancel_order",
                order_id="order-1",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
            scoped_cancel = management_adapter.manage_orders(
                "cancel_orders",
                market_id="market-1",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
        self.assertTrue(cancelled["response"]["success"])
        self.assertTrue(scoped_cancel["response"]["success"])
        self.assertEqual(management_calls[0][0], "DELETE")
        self.assertTrue(management_calls[0][1].endswith("/orders/order-1/"))
        self.assertEqual(management_calls[1][5], {"market_id": "market-1"})
        with self.assertRaises(MarketConfigurationError):
            management_adapter.manage_orders(
                "cancel_orders",
                market_id="../unsafe",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

        filled_activity = {**orders["orders"][1], "status": "filled"}
        copy_preview = live_adapter.copy_trade_from_activity(filled_activity)
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "market-1:contract-yes")
        self.assertEqual(copy_preview.raw["source"], "smarkets_authenticated_executed_orders")
        self.assertEqual(copy_preview.raw["trade_id"], "order-filled-1")
        self.assertTrue(copy_preview.raw["copied"])
        self.assertEqual(copy_preview.raw["activity"]["id"], "order-filled-1")
        self.assertAlmostEqual(copy_preview.average_price or 0.0, 0.45)
        self.assertEqual(copy_preview.raw["request"]["quantity"], "25000")
        self.assertEqual(copy_preview.raw["request"]["price"], "4500")
        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({**filled_activity, "status": "created"})
        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({**filled_activity, "side": "unknown"})
        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({**filled_activity, "contract_id": "other:contract-yes"})

    def test_context_v2_adapter_maps_markets_prices_orderbooks_paper_and_guarded_signed_orders(self) -> None:
        adapter = ContextV2Adapter()
        markets = load_fixture("context_v2", "markets")
        market = load_fixture("context_v2", "market")
        orderbook = load_fixture("context_v2", "orderbook")
        activity = load_fixture("context_v2", "activity")
        prices = load_fixture("context_v2", "prices")
        orders = load_fixture("context_v2", "orders")
        market_id = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        wallet = "0x3333333333333333333333333333333333333333"

        self.assertEqual(
            adapter.health_check()["order_management_operations"],
            ["cancel_order", "batch_cancel_orders"],
        )

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers, {"Authorization": "Bearer context-key"})
            if url.endswith("/markets"):
                return markets
            if url.endswith(f"/markets/{market_id}"):
                return market
            if url.endswith(f"/markets/{market_id}/orderbook"):
                return orderbook
            if url.endswith(f"/markets/{market_id}/activity"):
                self.assertEqual(params["types"], "trade")
                return activity
            if url.endswith(f"/markets/{market_id}/prices"):
                self.assertIn(params["timeframe"], {"1h", "1d", "1M"})
                return prices
            if url.endswith("/orders"):
                self.assertEqual(params["trader"], wallet)
                self.assertIn(params["status"], {"filled", "open"})
                return orders
            raise AssertionError(f"unexpected Context URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("context_v2", f"{market_id}:0", "BUY", 5, 0.44)
        with patch.dict("os.environ", {"CONTEXT_API_KEY": "context-key"}):
            events = adapter.list_events("BTC")
            contracts = adapter.list_contracts(market_id)
            book = adapter.get_orderbook(order.contract_id)
            price = adapter.get_price(order.contract_id)
            trades = adapter.list_trades(order.contract_id)
            yes_candles = adapter.list_candles(order.contract_id, resolution="1d")
            no_candles = adapter.list_candles(f"{market_id}:1", resolution="1M")
            paper = adapter.place_paper_order(order)
            activities = adapter.list_activity(wallet, limit=3)
            recovered = adapter.account_recovery("orders", wallet=wallet, limit=3)

        self.assertEqual(events[0].event_id, market_id)
        self.assertEqual([contract.outcome for contract in contracts], ["Yes", "No"])
        self.assertEqual([level.price for level in book.bids], [0.42, 0.4])
        self.assertEqual([level.price for level in book.asks], [0.46, 0.48])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.44)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "YES")
        self.assertAlmostEqual(trades[0].price, 0.44)
        self.assertEqual([c.close for c in yes_candles], [0.42, 0.44, 0.47])
        self.assertEqual([round(c.close, 2) for c in no_candles], [0.58, 0.56, 0.53])
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["request"]["marketId"], market_id)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["asset"], f"{market_id}:0")
        self.assertEqual(activities[0]["side"], "BUY")
        self.assertAlmostEqual(activities[0]["size"], 5.0)
        self.assertAlmostEqual(activities[0]["price"], 0.44)
        self.assertEqual(activities[0]["activityId"], f"context:{market_id}:0xabc1")
        self.assertEqual(recovered["orders"][0]["nonce"], "0xabc1")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = ContextV2Adapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "context_order_management_enabled": True,
            }
        )
        calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append((method, url, json, headers, timeout))
            if url.endswith("/orders/cancel"):
                return FakeResponse(load_fixture("context_v2", "cancel_response"))
            if url.endswith("/orders/bulk/cancel"):
                return FakeResponse(load_fixture("context_v2", "bulk_cancel_response"))
            return FakeResponse(load_fixture("context_v2", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        live_adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        signed_order = {
            "type": "limit",
            "marketId": market_id,
            "outcomeIndex": 0,
            "side": 0,
            "price": "440000",
            "size": "5000000",
            "trader": "0x3333333333333333333333333333333333333333",
            "nonce": "0x1",
            "signature": "0x" + "ab" * 65,
        }
        with patch.dict("os.environ", {"CONTEXT_API_KEY": "context-key"}):
            result = live_adapter.place_live_order(
                PaperOrderRequest(
                    "context_v2",
                    order.contract_id,
                    "BUY",
                    5,
                    0.44,
                    {"signed_order": signed_order},
                )
            )
        self.assertTrue(result["live"])
        self.assertTrue(result["response"]["success"])
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders"))
        self.assertEqual(calls[0][3]["Authorization"], "Bearer context-key")
        self.assertEqual(calls[0][2]["outcomeIndex"], 0)

        signed_cancel = {
            "trader": wallet,
            "nonce": "0xabc1",
            "signature": "0x" + "cd" * 65,
        }
        signed_cancel_2 = {
            "trader": wallet,
            "nonce": "0xabc2",
            "signature": "0x" + "ef" * 65,
        }
        with patch.dict("os.environ", {"CONTEXT_API_KEY": "context-key"}):
            cancelled = live_adapter.manage_orders(
                "cancel_order",
                signed_cancel=signed_cancel,
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
            batch_cancelled = live_adapter.manage_orders(
                "batch_cancel_orders",
                signed_cancel=[signed_cancel, signed_cancel_2],
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
        self.assertTrue(cancelled["response"]["success"])
        self.assertTrue(batch_cancelled["response"]["success"])
        self.assertTrue(calls[1][1].endswith("/orders/cancel"))
        self.assertEqual(calls[1][2], signed_cancel)
        self.assertTrue(calls[2][1].endswith("/orders/bulk/cancel"))
        self.assertEqual(calls[2][2]["cancels"], [signed_cancel, signed_cancel_2])
        with self.assertRaises(MarketConfigurationError):
            live_adapter.manage_orders(
                "cancel_order",
                signed_cancel={**signed_cancel, "trader": "0x1"},
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )
        with self.assertRaises(MarketConfigurationError):
            live_adapter.manage_orders(
                "batch_cancel_orders",
                signed_cancel=[signed_cancel, {**signed_cancel, "trader": "0x4444444444444444444444444444444444444444"}],
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

        copy_preview = live_adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(copy_preview.accepted)
        self.assertAlmostEqual(copy_preview.average_price or 0.0, 0.44)
        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({"asset": f"{market_id}:0", "side": "BUY", "size": 1, "status": "open"})

    def test_dflow_adapter_maps_nested_markets_orderbooks_paper_orders_and_guarded_rpc_submission(self) -> None:
        adapter = DFlowAdapter()
        events = load_fixture("dflow", "events")
        orderbook = load_fixture("dflow", "orderbook")
        trades = load_fixture("dflow", "trades")
        onchain_trades = load_fixture("dflow", "onchain_trades")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/api/v1/events"):
                self.assertEqual(headers, {})
                return events
            if url.endswith("/api/v1/orderbook/by-mint/mint-yes"):
                return orderbook
            raise AssertionError(f"unexpected DFlow URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        listed = adapter.list_events("bitcoin")
        contracts = adapter.list_contracts("KXBTC-26DEC31")
        order = PaperOrderRequest("dflow", "KXBTC-26DEC31-100K:mint-yes", "BUY", 5, 0.44)
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(listed[0].event_id, "KXBTC-26DEC31")
        self.assertEqual(
            [contract.contract_id for contract in contracts],
            ["KXBTC-26DEC31-100K:mint-yes", "KXBTC-26DEC31-100K:mint-no"],
        )
        self.assertEqual([level.price for level in book.bids], [0.42, 0.4])
        self.assertEqual([round(level.price, 6) for level in book.asks], [0.45, 0.47])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.435)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.raw["trade_request"]["outputMint"], "mint-yes")

        history_adapter = DFlowAdapter({"dflow_api_key": "dflow-key"})
        history_adapter._market_cache = adapter._market_cache

        def fake_history_get_json(url: str, *, params=None, headers=None):
            self.assertTrue(url.endswith("/api/v1/trades/by-mint/mint-yes"))
            self.assertEqual(params, {"limit": 2, "minTs": 1760000000, "maxTs": 1760010000})
            self.assertEqual(headers, {"x-api-key": "dflow-key"})
            return trades

        history_adapter.runtime.get_json = fake_history_get_json  # type: ignore[method-assign]
        history = history_adapter.list_trades(
            order.contract_id,
            limit=2,
            after=1760000000,
            before=1760010000,
        )
        self.assertEqual([trade.trade_id for trade in history], ["dflow-trade-1", "dflow-trade-2"])
        self.assertEqual(history[0].contract_id, order.contract_id)
        self.assertEqual(history[0].side, "YES")
        self.assertAlmostEqual(history[0].price, 0.42)
        self.assertAlmostEqual(history[0].size, 5.0)
        self.assertEqual(history[0].timestamp, 1760004000.0)

        candle_adapter = DFlowAdapter({"dflow_api_key": "dflow-key", "dflow_candle_trade_limit": 2})
        candle_adapter._market_cache = adapter._market_cache
        candle_adapter.runtime.get_json = fake_history_get_json  # type: ignore[method-assign]
        candles = candle_adapter.list_candles(
            order.contract_id,
            resolution="1h",
            from_timestamp=1760000000,
            to_timestamp=1760010000,
        )
        self.assertEqual(len(candles), 2)
        self.assertEqual([candle.timestamp for candle in candles], [1760004000.0, 1760007600.0])
        self.assertAlmostEqual(candles[0].open, 0.42)
        self.assertAlmostEqual(candles[0].volume or 0, 5.0)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(candles[0].raw["source"], "dflow_public_trade_feed")

        activity_adapter = DFlowAdapter({"dflow_api_key": "dflow-key"})
        activity_adapter._market_cache = adapter._market_cache

        def fake_activity_get_json(url: str, *, params=None, headers=None):
            self.assertTrue(url.endswith("/api/v1/onchain-trades"))
            self.assertEqual(
                params,
                {
                    "wallet": "11111111111111111111111111111111",
                    "limit": 2,
                    "sortBy": "createdAt",
                    "sortOrder": "desc",
                },
            )
            self.assertEqual(headers, {"x-api-key": "dflow-key"})
            return onchain_trades

        activity_adapter.runtime.get_json = fake_activity_get_json  # type: ignore[method-assign]
        activities = activity_adapter.list_activity("solana:11111111111111111111111111111111", limit=2)
        self.assertEqual([item["side"] for item in activities], ["BUY", "SELL"])
        self.assertEqual([item["outcome"] for item in activities], ["YES", "NO"])
        self.assertEqual(activities[0]["asset"], "KXBTC-26DEC31-100K:mint-yes")
        self.assertEqual(activities[0]["transactionHash"], "dflow-signature-1")
        self.assertEqual(activities[0]["proxyWallet"], "solana:11111111111111111111111111111111")
        copy_preview = activity_adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(copy_preview.accepted)
        self.assertAlmostEqual(copy_preview.average_price or 0.0, 0.44)
        recovered = activity_adapter.account_recovery(
            "account_activity",
            wallet="11111111111111111111111111111111",
            limit=2,
        )
        self.assertEqual(recovered["source"], "dflow_onchain_trades")
        self.assertEqual(len(recovered["activity"]), 2)

        with self.assertRaises(MarketConfigurationError):
            candle_adapter.list_candles(order.contract_id, resolution="2h")
        with self.assertRaises(MarketConfigurationError):
            candle_adapter.list_candles(order.contract_id, from_timestamp=10, to_timestamp=9)

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = DFlowAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "dflow_solana_rpc_url": "https://rpc.example",
            }
        )
        live_adapter._market_cache = adapter._market_cache
        calls = []

        def fake_request(method: str, url: str, *, params=None, json_body=None, headers=None, timeout=None):
            calls.append((method, url, params, json_body, headers, timeout))
            return load_fixture("dflow", "rpc_response")

        live_adapter.runtime.request_json = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"DFLOW_API_KEY": "dflow-key"}):
            result = live_adapter.place_live_order(
                PaperOrderRequest(
                    "dflow",
                    order.contract_id,
                    "BUY",
                    1,
                    0.44,
                    {"signed_transaction": "c2lnbmVk", "user_public_key": "wallet-1"},
                )
            )
        self.assertTrue(result["live"])
        self.assertEqual(result["response"]["result"], "signature-123")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "https://rpc.example")
        self.assertEqual(calls[0][3]["method"], "sendTransaction")

        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({"side": "BUY"})

    def test_matchbook_adapter_maps_events_markets_odds_paper_orders_and_guarded_offers(self) -> None:
        adapter = MatchbookAdapter()
        events = load_fixture("matchbook", "events")
        markets = load_fixture("matchbook", "markets")
        market = load_fixture("matchbook", "market")
        matched_bets = load_fixture("matchbook", "matched_bets")
        settled_bets = load_fixture("matchbook", "settled_bets")
        current_bets = load_fixture("matchbook", "current_bets")
        current_offers = load_fixture("matchbook", "current_offers")
        balance = load_fixture("matchbook", "balance")
        account = load_fixture("matchbook", "account")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/events"):
                return events
            if url.endswith("/events/101/markets"):
                return markets
            if url.endswith("/events/101/markets/202"):
                return market
            raise AssertionError(f"unexpected Matchbook URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("matchbook", "101:202:303", "BUY", 5, 0.5)

        listed = adapter.list_events("BTC")
        contracts = adapter.list_contracts("101")
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(listed[0].event_id, "101")
        self.assertEqual([contract.contract_id for contract in contracts], ["101:202:303", "101:202:304"])
        self.assertEqual([round(level.price, 6) for level in book.bids], [0.5, round(1 / 2.1, 6)])
        self.assertEqual([round(level.price, 6) for level in book.asks], [round(1 / 2.3, 6), round(1 / 2.2, 6)])
        self.assertAlmostEqual(price.midpoint or 0.0, (0.5 + 1 / 2.3) / 2)
        self.assertTrue(paper.accepted)

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = MatchbookAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, params=None, json=None, headers=None, timeout=None):
            calls.append((method, url, params, json, headers, timeout))
            if url.endswith("/security/session"):
                return FakeResponse(load_fixture("matchbook", "login_response"))
            if url.endswith("/v2/offers") and method == "POST":
                return FakeResponse(load_fixture("matchbook", "order_response"))
            if url.endswith("/v2/matched-bets/aggregated"):
                self.assertEqual(params["event-ids"], "101")
                self.assertEqual(params["market-ids"], "202")
                self.assertEqual(params["runner-ids"], "303")
                self.assertEqual(params["aggregation-type"], "average")
                return FakeResponse(matched_bets)
            if url.endswith("/reports/v2/bets/settled"):
                self.assertEqual(params["market-ids"], "202")
                self.assertTrue(params["after"].endswith("Z"))
                return FakeResponse(settled_bets)
            if url.endswith("/reports/v2/bets/current"):
                self.assertEqual(params["event-ids"], "101")
                return FakeResponse(current_bets)
            if url.endswith("/v2/offers") and method == "GET":
                self.assertEqual(params["status"], "open,matched")
                self.assertEqual(params["side"], "back")
                self.assertTrue(params["include-edits"])
                return FakeResponse(current_offers)
            if url.endswith("/account/balance"):
                return FakeResponse(balance)
            if url.endswith("/account"):
                return FakeResponse(account)
            raise AssertionError(f"unexpected Matchbook request URL: {url}")

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict(
            "os.environ",
            {"MATCHBOOK_USERNAME": "user", "MATCHBOOK_PASSWORD": "pass"},
        ):
            result = live_adapter.place_live_order(order)

        self.assertEqual(result["response"]["offers"][0]["id"], 404)
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/security/session"))
        self.assertEqual(calls[0][3]["username"], "user")
        self.assertTrue(calls[1][1].endswith("/v2/offers"))
        self.assertEqual(calls[1][3]["offers"][0]["runner-id"], 303)
        self.assertEqual(calls[1][3]["offers"][0]["odds"], 2.0)
        self.assertEqual(calls[1][4]["session-token"], "session-123")

        trades = live_adapter.list_trades(
            "101:202:303",
            limit=2,
            after=1780344000,
            before=1780344050,
        )
        candles = live_adapter.list_candles(
            "101:202:303",
            resolution="1h",
            from_timestamp=1780344000,
            to_timestamp=1780344050,
        )
        self.assertEqual([trade.trade_id for trade in trades], ["mb-303-1"])
        self.assertEqual(trades[0].side, "BUY")
        self.assertAlmostEqual(trades[0].price, 0.4)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp, 1780344000.0)
        self.assertAlmostEqual(candles[0].open, 0.4)
        self.assertAlmostEqual(candles[0].volume or 0, 4.0)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(candles[0].raw["source"], "matchbook_authenticated_matched_bets")

        settled = live_adapter.account_recovery(
            "settled_bets",
            market_id="202",
            limit=10,
            offset=2,
            from_timestamp=1780344000,
            to_timestamp=1780347600,
        )
        current = live_adapter.account_recovery("current_bets", event_id="101")
        offers = live_adapter.account_recovery(
            "current_offers",
            side="back",
            status="open,matched",
            include_edits=True,
            aggregation_type="summary",
        )
        self.assertEqual(settled["data"]["bets"][0]["id"], "settled-303-1")
        self.assertEqual(current["data"]["bets"][0]["id"], "current-303-1")
        self.assertEqual(offers["offers"][0]["id"], 405)
        self.assertEqual(live_adapter.account_recovery("balance")["balance"]["available"], 100.0)
        self.assertEqual(live_adapter.account_recovery("account")["id"], "account-1")

        with self.assertRaises(MarketConfigurationError):
            live_adapter.account_recovery("settled_bets", market_id="../outside")
        with self.assertRaises(MarketConfigurationError):
            live_adapter.account_recovery("current_offers", status="open,unknown")
        with self.assertRaises(MarketConfigurationError):
            live_adapter.account_recovery("current_offers", interval=-1)
        self.assertEqual(trades[0].size, 4.0)
        self.assertEqual(trades[0].timestamp, 1780344003.0)

        with self.assertRaises(MarketConfigurationError):
            live_adapter.list_trades("101:202:303", limit=1001)
        with self.assertRaises(MarketConfigurationError):
            live_adapter.list_trades("101:202:303", before=10, after=20)
        with self.assertRaises(MarketConfigurationError):
            live_adapter.list_candles("101:202:303", resolution="2h")

        copy_preview = live_adapter.copy_trade_from_activity(
            {
                **trades[0].raw,
                "contract_id": trades[0].contract_id,
                "side": trades[0].side,
                "price": trades[0].price,
                "size": trades[0].size,
                "trade_id": trades[0].trade_id,
            }
        )
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "101:202:303")
        self.assertEqual(copy_preview.raw["source"], "matchbook_authenticated_matched_bets")

    def test_matchbook_order_management_uses_fixed_cancel_and_edit_contracts(self) -> None:
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        adapter = MatchbookAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "matchbook_order_management_enabled": True,
            }
        )
        calls = []

        def fake_request(method: str, url: str, *, params=None, json=None, headers=None, timeout=None):
            calls.append((method, url, params, json, headers, timeout))
            if method == "DELETE" and url.endswith("/v2/offers/404"):
                return FakeResponse(load_fixture("matchbook", "cancel_offer_response"))
            if method == "DELETE" and url.endswith("/v2/offers"):
                if params and params.get("offer-ids") == "404,405":
                    return FakeResponse(load_fixture("matchbook", "cancel_offers_response"))
                return FakeResponse(load_fixture("matchbook", "cancel_all_offers_response"))
            if method == "PUT" and url.endswith("/v2/offers/404"):
                return FakeResponse(load_fixture("matchbook", "edit_offer_response"))
            if method == "PUT" and url.endswith("/v2/offers"):
                return FakeResponse(load_fixture("matchbook", "edit_offers_response"))
            raise AssertionError(f"unexpected Matchbook mutation request: {method} {url}")

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"MATCHBOOK_SESSION_TOKEN": "session-123"}):
            cancelled = adapter.manage_orders(
                "cancel_offer",
                order_id=404,
                confirm_order_management=confirmation,
            )
            cancelled_batch = adapter.manage_orders(
                "cancel_offers",
                offer_ids=[404, 405],
                confirm_order_management=confirmation,
            )
            with self.assertRaises(MarketConfigurationError):
                adapter.manage_orders(
                    "cancel_all_offers",
                    confirm_order_management=confirmation,
                    confirm_global_cancel="wrong",
                )
            cancelled_all = adapter.manage_orders(
                "cancel_all_offers",
                confirm_order_management=confirmation,
                confirm_global_cancel="CANCEL ALL MATCHBOOK OFFERS",
            )
            edited = adapter.manage_orders(
                "edit_offer",
                order_id=404,
                current_odds=1.5,
                new_odds=2.0,
                current_stake=5,
                new_stake=6,
                confirm_order_management=confirmation,
            )
            edited_batch = adapter.manage_orders(
                "edit_offers",
                offers=[
                    {
                        "id": 404,
                        "current-odds": 1.5,
                        "new-odds": 2.0,
                        "current-stake": 5,
                        "new-stake": 6,
                    }
                ],
                confirm_order_management=confirmation,
            )

        self.assertTrue(cancelled["live"])
        self.assertEqual(cancelled["response"]["offers"][0]["status"], "cancelled")
        self.assertEqual(cancelled_batch["response"]["offers"][0]["id"], 404)
        self.assertEqual(cancelled_all["response"]["cancelled"], "all")
        self.assertEqual(edited["request"]["body"]["new-odds"], 2.0)
        self.assertEqual(edited_batch["request"]["body"]["offers"][0]["id"], 404)
        self.assertEqual(calls[0][0], "DELETE")
        self.assertTrue(calls[0][1].endswith("/v2/offers/404"))
        self.assertEqual(calls[1][2]["offer-ids"], "404,405")
        self.assertEqual(calls[2][0], "DELETE")
        self.assertEqual(calls[2][2], None)
        self.assertEqual(calls[3][0], "PUT")
        self.assertEqual(calls[3][3]["current-odds"], 1.5)
        self.assertEqual(calls[4][0], "PUT")
        self.assertEqual(calls[4][3]["offers"][0]["new-stake"], 6.0)
        self.assertEqual(calls[0][4]["session-token"], "session-123")
        self.assertEqual(adapter.health_check()["order_management_operations"], [
            "cancel_offer",
            "cancel_offers",
            "cancel_all_offers",
            "edit_offer",
            "edit_offers",
        ])
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_offers", offer_ids=[404, 404], confirm_order_management=confirmation)
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("edit_offers", offers=[{"id": 404}], confirm_order_management=confirmation)

    def test_probable_adapter_maps_events_tokens_orderbooks_paper_orders_and_guarded_signed_orders(self) -> None:
        adapter = ProbableAdapter()
        events = load_fixture("probable", "events")
        event = load_fixture("probable", "event")
        market = load_fixture("probable", "market")
        orderbook = load_fixture("probable", "orderbook")
        order_response = load_fixture("probable", "order_response")
        activity = load_fixture("probable", "activity")
        prices_history = load_fixture("probable", "prices_history")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/events"):
                return events
            if url.endswith("/events/event-1"):
                return event
            if url.endswith("/markets/market-1"):
                return market
            if url.endswith("/book"):
                self.assertEqual(params["token_id"], "token-yes")
                return orderbook
            if url.endswith("/activity"):
                self.assertEqual(params["user"], "0x0000000000000000000000000000000000000001")
                self.assertEqual(params["type"], ["TRADE"])
                if "market" in params:
                    self.assertEqual(params["market"], ["condition-1"])
                return activity
            if url.endswith("/prices-history"):
                self.assertEqual(params["market"], "token-yes")
                self.assertEqual(params["interval"], "1h")
                self.assertEqual(params["startTs"], 1780344000000)
                self.assertEqual(params["endTs"], 1780347600000)
                return prices_history
            raise AssertionError(f"unexpected Probable URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("probable", "market-1:token-yes", "BUY", 5, 0.44)

        events_result = adapter.list_events("BTC")
        contracts = adapter.list_contracts("event-1")
        book = adapter.get_orderbook(order.contract_id)
        price = adapter.get_price(order.contract_id)
        paper = adapter.place_paper_order(order)

        self.assertEqual(events_result[0].event_id, "event-1")
        self.assertEqual([contract.contract_id for contract in contracts], ["market-1:token-yes", "market-1:token-no"])
        self.assertEqual([level.price for level in book.bids], [0.42, 0.4])
        self.assertEqual([level.price for level in book.asks], [0.45, 0.47])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.435)
        self.assertTrue(paper.accepted)

        history_adapter = ProbableAdapter(
            {"probable_address": "0x0000000000000000000000000000000000000001"}
        )
        history_adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        trades = history_adapter.list_trades(
            "market-1:token-yes",
            limit=1,
            after=1780344000,
            before=1780344004,
        )
        candles = history_adapter.list_candles(
            "market-1:token-yes",
            resolution="1h",
            from_timestamp=1780344000,
            to_timestamp=1780347600,
        )
        activities = history_adapter.list_activity(
            "0x0000000000000000000000000000000000000001",
            limit=2,
        )

        self.assertEqual([trade.trade_id for trade in trades], ["0xprobabletrade1"])
        self.assertEqual([trade.side for trade in trades], ["BUY"])
        self.assertEqual([trade.price for trade in trades], [0.44])
        self.assertEqual([trade.size for trade in trades], [4.0])
        self.assertEqual([trade.timestamp for trade in trades], [1780344003.0])
        self.assertEqual([candle.close for candle in candles], [0.42, 0.44])
        self.assertEqual([candle.volume for candle in candles], [None, None])
        self.assertEqual([item["asset"] for item in activities], ["market-1:token-yes", "market-1:token-no"])
        self.assertEqual([item["side"] for item in activities], ["BUY", "SELL"])
        self.assertEqual([item["source"] for item in activities], ["probable_public_activity"] * 2)
        copy_preview = history_adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(copy_preview.accepted)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("market-1:token-yes")
        with self.assertRaises(MarketConfigurationError):
            ProbableAdapter({"probable_address": "not-an-address"}).list_trades("market-1:token-yes")
        with self.assertRaises(MarketConfigurationError):
            history_adapter.list_candles("market-1:token-yes", resolution="5m")
        with self.assertRaises(MarketConfigurationError):
            history_adapter.list_trades("market-1:token-yes", before=10, after=20)
        with self.assertRaises(MarketConfigurationError):
            history_adapter.list_activity("not-an-address")
        with self.assertRaises(MarketConfigurationError):
            history_adapter.copy_trade_from_activity({"asset": "market-1:token-yes", "side": "BUY", "size": 0})

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(order)

        live_adapter = ProbableAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            return FakeResponse(order_response)

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        live_adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        signed_order = {
            "salt": "1",
            "maker": "0x0000000000000000000000000000000000000001",
            "signer": "0x0000000000000000000000000000000000000001",
            "taker": "0x0000000000000000000000000000000000000000",
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
        live_order = PaperOrderRequest(
            "probable",
            "market-1:token-yes",
            "BUY",
            5,
            0.44,
            {"signed_order": signed_order},
        )
        with patch.dict(
            "os.environ",
            {
                "PROB_ADDRESS": "0x0000000000000000000000000000000000000001",
                "PROB_API_KEY": "prob-key",
                "PROB_API_SECRET": "c2VjcmV0",
                "PROB_PASSPHRASE": "prob-pass",
            },
        ):
            result = live_adapter.place_live_order(live_order)

        self.assertEqual(result["response"]["orderId"], 123)
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders/56"))
        self.assertEqual(calls[0][3]["PROB_API_KEY"], "prob-key")
        self.assertTrue(calls[0][3]["PROB_SIGNATURE"])
        self.assertEqual(json.loads(calls[0][2])["order"]["tokenId"], "token-yes")

        with self.assertRaises(MarketConfigurationError):
            live_adapter.copy_trade_from_activity({"side": "BUY"})

    def test_probable_account_reads_and_guarded_cancellations_use_fixed_signed_paths(self) -> None:
        adapter = ProbableAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "probable_order_management_enabled": True,
                "probable_address": "0x0000000000000000000000000000000000000001",
                "probable_api_key": "prob-key",
                "probable_api_secret": "c2VjcmV0",
                "probable_api_passphrase": "prob-pass",
            }
        )
        open_orders = load_fixture("probable", "open_orders")
        order_detail = load_fixture("probable", "order_detail")
        cancel_response = load_fixture("probable", "cancel_order_response")
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            if method == "GET" and "/orders/56/open?" in url:
                return FakeResponse(open_orders)
            if method == "GET" and "/order/56/123?tokenId=token-yes" in url:
                return FakeResponse(order_detail)
            if method == "DELETE":
                return FakeResponse(cancel_response)
            raise AssertionError(f"unexpected Probable request: {method} {url}")

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        self.assertEqual(adapter.health_check()["account_recovery_operations"], ["open_orders", "order"])
        self.assertEqual(
            adapter.health_check()["order_management_operations"],
            ["cancel_order", "cancel_orders", "cancel_all_orders"],
        )

        open_result = adapter.account_recovery(
            "open_orders",
            page=2,
            limit=2,
            event_id="162",
            token_ids=["token-yes"],
        )
        detail_result = adapter.account_recovery("order", order_id="123", token_id="token-yes")
        self.assertEqual(open_result["orders"][0]["orderId"], 123)
        self.assertEqual(detail_result["status"], "OPEN")
        self.assertIn("PROB_SIGNATURE", calls[0][3])
        self.assertIn("eventId=162", calls[0][1])
        self.assertIn("tokenIds=token-yes", calls[0][1])

        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        single = adapter.manage_orders(
            "cancel_order",
            order_id="123",
            token_id="token-yes",
            confirm_order_management=confirmation,
        )
        batch = adapter.manage_orders(
            "cancel_orders",
            order_ids=["123", "124"],
            token_id="token-yes",
            confirm_order_management=confirmation,
        )
        all_orders = adapter.manage_orders(
            "cancel_all_orders",
            confirm_order_management=confirmation,
            confirm_global_cancel="CANCEL ALL PROBABLE ORDERS",
        )
        self.assertEqual(single["response"]["status"], "CANCELED")
        self.assertEqual(len(batch["response"]), 2)
        self.assertEqual(len(all_orders["response"]), 2)
        delete_urls = [call[1] for call in calls if call[0] == "DELETE"]
        self.assertTrue(delete_urls)
        self.assertTrue(all("/order/56/" in url and "tokenId=" in url for url in delete_urls))

        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_all_orders",
                confirm_order_management=confirmation,
                confirm_global_cancel="wrong",
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_order",
                order_id="../outside",
                token_id="token-yes",
                confirm_order_management=confirmation,
            )

    def test_xmarket_adapter_maps_markets_orderbooks_paper_orders_and_guarded_live_orders(self) -> None:
        adapter = XMarketAdapter()
        markets = load_fixture("xmarket", "markets")
        market = load_fixture("xmarket", "market")
        orderbook = load_fixture("xmarket", "orderbook")
        positions = load_fixture("xmarket", "positions")
        user_orders = load_fixture("xmarket", "user_orders")
        market_orders = load_fixture("xmarket", "market_orders")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers["x-api-key"], "xmarket-key")
            if url.endswith("/markets"):
                return markets
            if url.endswith("/markets/market-1"):
                return market
            if url.endswith("/orderbook/outcome-yes"):
                return orderbook
            if url.endswith("/positions"):
                self.assertIn(params, ({"status": "open", "page": 1, "pageSize": 50}, {"status": "closed", "page": 2, "pageSize": 25}))
                return positions
            if url.endswith("/openapi/v1/order/my-orders"):
                self.assertEqual(params["status"], "all")
                self.assertEqual(params["page"], 1)
                self.assertGreaterEqual(params["pageSize"], 1)
                self.assertLessEqual(params["pageSize"], 100)
                return user_orders
            if url.endswith("/openapi/v1/order/market/market-1"):
                self.assertEqual(params, {"status": "open", "page": 2, "pageSize": 25})
                return market_orders
            raise AssertionError(f"unexpected Xmarket URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("xmarket", "market-1:outcome-yes", "BUY", 10, 0.44)

        with patch.dict("os.environ", {"XMARKET_API_KEY": "xmarket-key"}):
            events = adapter.list_events("election")
            contracts = adapter.list_contracts("market-1")
            book = adapter.get_orderbook("market-1:outcome-yes")
            price = adapter.get_price("market-1:outcome-yes")
            paper = adapter.place_paper_order(order)
            position_rows = adapter.account_recovery("positions")
            order_rows = adapter.account_recovery("user_orders")
            market_order_rows = adapter.account_recovery(
                "market_orders",
                market_id="market-1",
                status="open",
                page=2,
                limit=25,
            )
            trade_history = adapter.list_trades("market-1:outcome-yes", limit=10)
            candle_history = adapter.list_candles("market-1:outcome-yes", resolution="1h")
            copy_preview = adapter.copy_trade_from_activity(user_orders["items"][1])
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(order)

        self.assertEqual(events[0].event_id, "market-1")
        self.assertEqual([contract.contract_id for contract in contracts], ["market-1:outcome-yes", "market-1:outcome-no"])
        self.assertEqual([level.price for level in book.bids], [0.43, 0.41])
        self.assertEqual([level.price for level in book.asks], [0.45, 0.47])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.44)
        self.assertTrue(paper.accepted)
        self.assertEqual(position_rows["items"][0]["id"], "position-1")
        self.assertEqual(order_rows["items"][0]["id"], "xorder-1")
        self.assertEqual(market_order_rows["items"][0]["marketId"], "market-1")
        self.assertEqual([trade.trade_id for trade in trade_history], ["xorder-filled-1"])
        self.assertEqual(trade_history[0].side, "BUY")
        self.assertAlmostEqual(trade_history[0].price, 0.45)
        self.assertAlmostEqual(trade_history[0].size, 4.0)
        self.assertTrue(trade_history[0].raw["account_scoped"])
        self.assertEqual(len(candle_history), 1)
        self.assertAlmostEqual(candle_history[0].open, 0.45)
        self.assertAlmostEqual(candle_history[0].close, 0.45)
        self.assertAlmostEqual(candle_history[0].volume or 0.0, 4.0)
        self.assertTrue(candle_history[0].raw["derived"])
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "market-1:outcome-yes")
        self.assertEqual(copy_preview.raw["request"]["quantity"], 4.0)
        self.assertAlmostEqual(copy_preview.raw["request"]["price"], 0.45)
        self.assertEqual(copy_preview.raw["source"], "xmarket_authenticated_my_orders")
        self.assertTrue(copy_preview.raw["account_scoped"])

        for invalid_activity in (
            {**user_orders["items"][1], "status": "open"},
            {**user_orders["items"][1], "side": "unknown"},
            {**user_orders["items"][1], "filledQuantity": 0},
            {**user_orders["items"][1], "averagePrice": 1.0},
            {**user_orders["items"][1], "id": ""},
        ):
            with self.assertRaises(MarketConfigurationError):
                adapter.copy_trade_from_activity(invalid_activity)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("market-1:outcome-yes", limit=101)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("market-1:outcome-yes", before=10, after=20)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("market-1:outcome-yes", resolution="2h")
        self.assertTrue(adapter.health_check()["trade_history_account_scoped"])
        self.assertTrue(adapter.health_check()["candle_history_derived"])

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("market_orders", market_id="../private")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("positions", status="unknown")

        live_adapter = XMarketAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, params=None, json=None, headers=None, timeout=None):
            calls.append((method, url, params, json, headers, timeout))
            return FakeResponse(load_fixture("xmarket", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"XMARKET_API_KEY": "xmarket-key"}):
            result = live_adapter.place_live_order(order)

        self.assertEqual(result["response"]["id"], "xorder-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/openapi/v1/order"))
        self.assertEqual(calls[0][3]["outcomeId"], "outcome-yes")
        self.assertEqual(calls[0][4]["x-api-key"], "xmarket-key")

        batch_adapter = XMarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "xmarket_order_management_enabled": True,
            }
        )
        batch_calls = []

        def fake_batch_request(method: str, url: str, *, params=None, json=None, headers=None, timeout=None):
            batch_calls.append((method, url, params, json, headers, timeout))
            if url.endswith("/openapi/v1/order/batch"):
                return FakeResponse(load_fixture("xmarket", "batch_order_response"))
            if url.endswith("/openapi/v1/order/cancel-batch"):
                return FakeResponse(load_fixture("xmarket", "batch_cancel_response"))
            raise AssertionError(f"unexpected Xmarket mutation URL: {url}")

        batch_adapter.runtime.session.request = fake_batch_request  # type: ignore[method-assign]
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        with patch.dict("os.environ", {"XMARKET_API_KEY": "xmarket-key"}):
            created = batch_adapter.manage_orders(
                "batch_create_orders",
                orders=[
                    {"outcomeId": "outcome-yes", "side": "buy", "type": "limit", "price": 0.44, "quantity": 10},
                    {"outcome_id": "outcome-no", "side": "sell", "type": "market", "quantity": 5},
                ],
                confirm_order_management=confirmation,
            )
            cancelled = batch_adapter.manage_orders(
                "batch_cancel_orders",
                orders=["xorder-2", "xorder-3"],
                confirm_order_management=confirmation,
            )

        self.assertEqual(created["response"]["orders"][0]["id"], "xorder-2")
        self.assertEqual(created["request"]["orders"][1], {"outcomeId": "outcome-no", "side": "sell", "type": "market", "quantity": 5.0})
        self.assertEqual(cancelled["response"]["cancelled"], ["xorder-2", "xorder-3"])
        self.assertEqual([call[0] for call in batch_calls], ["POST", "POST"])
        self.assertTrue(batch_calls[0][1].endswith("/openapi/v1/order/batch"))
        self.assertTrue(batch_calls[1][1].endswith("/openapi/v1/order/cancel-batch"))
        self.assertEqual(batch_calls[1][3], {"orderIds": ["xorder-2", "xorder-3"]})
        self.assertEqual(batch_adapter.health_check()["order_management_operations"], ["batch_create_orders", "batch_cancel_orders"])

        with self.assertRaises(MarketConfigurationError):
            batch_adapter.manage_orders(
                "batch_cancel_orders",
                orders=["../outside"],
                confirm_order_management=confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            batch_adapter.manage_orders(
                "batch_create_orders",
                orders=[{"outcomeId": "outcome-yes", "side": "buy", "type": "limit", "quantity": 1}],
                confirm_order_management=confirmation,
            )

    def test_gemini_prediction_adapter_maps_events_contracts_orderbook_and_paper_orders(self) -> None:
        adapter = GeminiPredictionAdapter()
        events = load_fixture("gemini", "events")
        event = load_fixture("gemini", "event")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/v1/prediction-markets/events"):
                return events
            if url.endswith("/v1/prediction-markets/events/BTC100K2026"):
                return event
            raise AssertionError(f"unexpected Gemini URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        listed = adapter.list_events("bitcoin")
        contracts = adapter.list_contracts("BTC100K2026")
        book = adapter.get_orderbook("BTC100K2026:GEMI-BTC100K26-YES")
        price = adapter.get_price("BTC100K2026:GEMI-BTC100K26-YES")
        candles = adapter.list_candles(
            "BTC100K2026:GEMI-BTC100K26-YES",
            resolution="raw",
            from_timestamp=1787382000,
            to_timestamp=1787385600,
        )
        paper = adapter.place_paper_order(
            PaperOrderRequest("gemini_titan", "BTC100K2026:GEMI-BTC100K26-YES", "BUY", 3, 0.44)
        )

        self.assertEqual(listed[0].event_id, "BTC100K2026")
        self.assertEqual([contract.outcome for contract in contracts], ["Yes", "No"])
        self.assertEqual([level.price for level in book.bids], [0.42, 0.4])
        self.assertEqual([level.price for level in book.asks], [0.45, 0.47])
        self.assertAlmostEqual(price.last or 0.0, 0.44)
        self.assertAlmostEqual(price.midpoint or 0.0, 0.435)
        self.assertEqual([candle.timestamp for candle in candles], [1787382000.0, 1787385600.0])
        self.assertEqual([candle.close for candle in candles], [0.4, 0.44])
        self.assertEqual([candle.volume for candle in candles], [None, None])
        self.assertTrue(paper.accepted)

        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(
                PaperOrderRequest("gemini_titan", "BTC100K2026:GEMI-BTC100K26-YES", "BUY", 3, 0.44)
            )

        live_adapter = GeminiPredictionAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            if url.endswith("/v1/prediction-markets/terms/status"):
                return FakeResponse({"hasAcceptedLatest": True, "acceptedVersion": 3, "latestVersion": 3})
            return FakeResponse(load_fixture("gemini", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key", "GEMINI_API_SECRET": "gemini-secret"}):
            result = live_adapter.place_live_order(
                PaperOrderRequest(
                    "gemini_titan",
                    "BTC100K2026:GEMI-BTC100K26-YES",
                    "BUY",
                    3,
                    0.44,
                    {"nonce": 123, "client_order_id": "client-1", "outcome": "yes"},
                )
            )

        self.assertEqual(result["response"]["orderId"], 106817811)
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].endswith("/v1/prediction-markets/terms/status"))
        self.assertEqual(calls[1][0], "POST")
        self.assertTrue(calls[1][1].endswith("/v1/prediction-markets/order"))
        request_payload = json.loads(calls[1][2])
        self.assertEqual(request_payload["request"], "/v1/prediction-markets/order")
        self.assertEqual(request_payload["symbol"], "GEMI-BTC100K26-YES")
        self.assertEqual(request_payload["quantity"], "3")
        self.assertEqual(request_payload["outcome"], "yes")
        self.assertEqual(calls[1][3]["X-GEMINI-APIKEY"], "gemini-key")
        self.assertEqual(calls[1][3]["Content-Type"], "application/json")
        self.assertTrue(calls[1][3]["X-GEMINI-SIGNATURE"])

    def test_gemini_live_order_fails_closed_when_terms_are_not_accepted(self) -> None:
        adapter = GeminiPredictionAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            return FakeResponse({"hasAcceptedLatest": False, "acceptedVersion": 2, "latestVersion": 3})

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key", "GEMINI_API_SECRET": "gemini-secret"}):
            with self.assertRaisesRegex(MarketConfigurationError, "terms are not accepted"):
                adapter.place_live_order(
                    PaperOrderRequest(
                        "gemini_titan",
                        "BTC100K2026:GEMI-BTC100K26-YES",
                        "BUY",
                        3,
                        0.44,
                        {"outcome": "yes"},
                    )
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].endswith("/v1/prediction-markets/terms/status"))

    def test_gemini_prediction_authenticated_recovery_uses_documented_post_contracts(self) -> None:
        adapter = GeminiPredictionAdapter()
        calls = []
        fixture_by_path = {
            "/v1/prediction-markets/orders/active": "active_orders",
            "/v1/prediction-markets/orders/history": "order_history",
            "/v1/prediction-markets/positions": "positions",
            "/v1/prediction-markets/positions/settled": "settled_positions",
            "/v1/prediction-markets/metrics/volume": "volume_metrics",
        }

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            path = "/" + url.split("/", 3)[-1]
            calls.append((method, path, json.loads(data), headers, timeout))
            return FakeResponse(load_fixture("gemini", fixture_by_path[path]))

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key", "GEMINI_API_SECRET": "gemini-secret"}):
            active = adapter.list_active_orders(
                "BTC100K2026:GEMI-BTC100K26-YES",
                limit=50,
                offset=2,
            )
            history = adapter.list_order_history(
                status="filled",
                contract_id="BTC100K2026:GEMI-BTC100K26-YES",
                limit=50,
                offset=0,
                from_timestamp=1787385600,
                to_timestamp=1787389200,
            )
            trades = adapter.list_trades(
                "BTC100K2026:GEMI-BTC100K26-YES",
                limit=50,
                after=1787385600,
                before=1787389200,
            )
            copy_preview = adapter.copy_trade_from_activity(
                {
                    **history["orders"][0],
                    "contract_id": "BTC100K2026:GEMI-BTC100K26-YES",
                }
            )
            positions = adapter.get_positions(
                "BTC100K2026",
                limit=100,
                offset=2,
                sort="-unrealizedPnl",
            )
            settled = adapter.get_settled_positions(
                "BTC100K2025",
                limit=100,
                offset=1,
                sort="-date",
                search="Bitcoin",
                category="crypto",
                with_cash_outs=True,
            )
            volume = adapter.get_volume_metrics(
                "BTC100K2026",
                start_timestamp=1787385600,
                end_timestamp=1787389200,
            )

        self.assertEqual(active["orders"][0]["orderId"], "order-1001")
        self.assertEqual(history["orders"][0]["status"], "filled")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].trade_id, "order-0999")
        self.assertEqual(trades[0].side, "BUY")
        self.assertEqual(trades[0].size, 2.0)
        self.assertAlmostEqual(trades[0].price, 0.41)
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "BTC100K2026:GEMI-BTC100K26-YES")
        self.assertEqual(copy_preview.average_price, 0.41)
        self.assertEqual(copy_preview.raw["source"], "gemini_authenticated_filled_orders")
        self.assertEqual(copy_preview.raw["order_id"], "order-0999")
        self.assertEqual(positions["positions"][0]["symbol"], "GEMI-BTC100K26-YES")
        self.assertEqual(settled["positions"][0]["settlementStatus"], "settled")
        self.assertEqual(volume["eventTicker"], "BTC100K2026")
        self.assertEqual([call[0] for call in calls], ["POST"] * 6)
        self.assertEqual(
            [call[1] for call in calls],
            [
                "/v1/prediction-markets/orders/active",
                "/v1/prediction-markets/orders/history",
                "/v1/prediction-markets/orders/history",
                "/v1/prediction-markets/positions",
                "/v1/prediction-markets/positions/settled",
                "/v1/prediction-markets/metrics/volume",
            ],
        )
        self.assertEqual(calls[0][2]["symbol"], "GEMI-BTC100K26-YES")
        self.assertEqual(calls[0][2]["offset"], 2)
        self.assertEqual(calls[1][2]["status"], "filled")
        self.assertEqual(calls[1][2]["from"], 1787385600000)
        self.assertEqual(calls[1][2]["to"], 1787389200000)
        self.assertEqual(calls[2][2]["status"], "filled")
        self.assertEqual(calls[2][2]["symbol"], "GEMI-BTC100K26-YES")
        self.assertEqual(calls[3][2]["sort"], "-unrealizedPnl")
        self.assertTrue(calls[4][2]["withCashOuts"])
        self.assertEqual(calls[5][2]["startTime"], 1787385600000)
        self.assertEqual(calls[5][2]["endTime"], 1787389200000)
        self.assertTrue(all(call[3]["X-GEMINI-APIKEY"] == "gemini-key" for call in calls))
        self.assertTrue(all("gemini-secret" not in json.dumps(call[2]) for call in calls))

        with self.assertRaises(MarketConfigurationError):
            adapter.list_order_history(status="open")
        with self.assertRaises(MarketConfigurationError):
            adapter.get_positions(offset=1)
        with self.assertRaises(MarketConfigurationError):
            adapter.get_settled_positions(sort="-payouts")
        with self.assertRaises(MarketConfigurationError):
            adapter.get_volume_metrics("../BTC100K2026")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_order_history(from_timestamp=1787389200, to_timestamp=1787385600)
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**history["orders"][0], "status": "cancelled"})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity(
                {
                    **history["orders"][0],
                    "contract_id": "BTC100K2026:GEMI-BTC100K26-YES",
                    "filledQuantity": 0,
                }
            )

    def test_gemini_prediction_order_management_uses_fixed_cancel_contracts_and_guards(self) -> None:
        adapter = GeminiPredictionAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "gemini_order_management_enabled": True,
            }
        )
        calls = []
        fixture_by_path = {
            "/v1/prediction-markets/order/cancel": "cancel_order_response",
            "/v1/prediction-markets/order/batch/cancel": "batch_cancel_orders_response",
        }

        def fake_request(method: str, url: str, *, data=None, headers=None, timeout=None):
            path = "/" + url.split("/", 3)[-1]
            calls.append((method, path, json.loads(data), headers, timeout))
            return FakeResponse(load_fixture("gemini", fixture_by_path[path]))

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key", "GEMINI_API_SECRET": "gemini-secret"}):
            single = adapter.manage_orders("cancel_order", order_id="106817811", confirm_order_management=confirmation)
            batch = adapter.manage_orders(
                "batch_cancel_orders",
                orders=["106817811", 106817812],
                confirm_order_management=confirmation,
            )

        self.assertEqual(single["response"]["result"], "ok")
        self.assertEqual(batch["response"]["results"][1]["error"], "OrderNotFound")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        self.assertEqual(
            [call[1] for call in calls],
            [
                "/v1/prediction-markets/order/cancel",
                "/v1/prediction-markets/order/batch/cancel",
            ],
        )
        self.assertEqual(calls[0][2]["orderId"], 106817811)
        self.assertEqual(calls[1][2]["orderIds"], [106817811, 106817812])
        self.assertTrue(all(call[3]["X-GEMINI-APIKEY"] == "gemini-key" for call in calls))
        self.assertTrue(all("gemini-secret" not in json.dumps(call[2]) for call in calls))

        health = adapter.health_check()
        self.assertEqual(health["order_management_operations"], ["cancel_order", "batch_cancel_orders"])
        self.assertTrue(health["order_management_enabled"])
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", order_id="../private", confirm_order_management=confirmation)
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "batch_cancel_orders",
                orders=[1, 1],
                confirm_order_management=confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", order_id=1, confirm_order_management="wrong")

    def test_myriad_adapter_maps_official_events_outcomes_prices_orderbooks_and_dry_run_quotes(self) -> None:
        adapter = MyriadAdapter()
        events = load_fixture("myriad_markets", "events")
        event = load_fixture("myriad_markets", "event")
        market = load_fixture("myriad_markets", "market")
        orderbook = load_fixture("myriad_markets", "orderbook")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/markets"):
                self.assertEqual(params["group_by_event"], True)
                self.assertEqual(params["trading_model"], "all")
                return events
            if url.endswith("/events/event-10"):
                return event
            if url.endswith("/markets/501"):
                return market
            if url.endswith("/markets/501/orderbook"):
                self.assertEqual(params["outcome"], 1)
                return orderbook
            raise AssertionError(f"unexpected Myriad URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        events = adapter.list_events("BTC")
        contracts = adapter.list_contracts("event-10")
        standalone_contracts = adapter.list_contracts("501")
        price = adapter.get_price("501:1")
        book = adapter.get_orderbook("501:1")
        candles = adapter.list_candles(
            "501:1",
            resolution="7d",
            from_timestamp=1719740000,
            to_timestamp=1719840000,
        )
        paper = adapter.place_paper_order(PaperOrderRequest("myriad_markets", "501:1", "BUY", 20))

        self.assertEqual(events[0].event_id, "event-10")
        self.assertEqual([contract.contract_id for contract in contracts], ["501:1", "501:2"])
        self.assertEqual([contract.contract_id for contract in standalone_contracts], ["501:1", "501:2"])
        self.assertEqual(price.last, 0.61)
        self.assertEqual(price.bid, 0.60)
        self.assertEqual(price.ask, 0.62)
        self.assertEqual(price.midpoint, 0.61)
        self.assertEqual([level.price for level in book.bids], [0.62, 0.6])
        self.assertEqual([level.size for level in book.asks], [2.0, 1.0])
        self.assertEqual([candle.timestamp for candle in candles], [1719748800.0, 1719835200.0])
        self.assertEqual([candle.close for candle in candles], [0.54, 0.61])
        self.assertEqual([candle.volume for candle in candles], [44.0, None])
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("501:1", resolution="5sec")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("501:1", from_timestamp=1719840000, to_timestamp=1719740000)
        self.assertEqual(paper.raw["request"]["action"], "buy")
        self.assertEqual(paper.raw["request"]["value"], 20.0)
        with self.assertRaises(MarketConfigurationError):
            adapter.place_live_order(PaperOrderRequest("myriad_markets", "501:1", "BUY", 20))

        live_adapter = MyriadAdapter(
            {"live_trading_enabled": True, "live_trading_confirmed": True, "myriad_network_id": 56}
        )
        calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append((method, url, json, headers, timeout))
            return FakeResponse(load_fixture("myriad_markets", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict(
            "os.environ",
            {"MYRIAD_API_KEY": "myriad-key", "MYRIAD_API_SECRET": "myriad-secret"},
        ):
            result = live_adapter.place_live_order(
                PaperOrderRequest(
                    "myriad_markets",
                    "501:1",
                    "BUY",
                    20,
                    0.62,
                    {
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
                    },
                )
            )

        self.assertEqual(result["response"]["orderHash"], "0xmyriadorder")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders"))
        self.assertEqual(calls[0][2]["network_id"], 56)
        self.assertEqual(calls[0][3]["x-api-key"], "myriad-key")
        self.assertIn("x-api-timestamp", calls[0][3])
        self.assertRegex(calls[0][3]["x-api-signature"], r"^[0-9a-f]{64}$")

    def test_myriad_order_management_uses_signed_fixed_cancel_and_modify_contracts(self) -> None:
        adapter = MyriadAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "myriad_order_management_enabled": True,
                "myriad_network_id": 56,
            }
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        global_confirmation = "CANCEL ALL MYRIAD ORDERS"
        signed_order = {
            "trader": "0x1234567890123456789012345678901234567890",
            "marketId": "42",
            "outcomeId": 0,
            "side": 0,
            "amount": "1000000000000000000",
            "price": "500000000000000000",
            "minFillAmount": "0",
            "nonce": "1",
            "expiration": "0",
        }
        entry = {"order": signed_order, "signature": "0x" + "ab" * 65}
        responses = {
            "/orders/0x" + "12" * 32: {"orderHash": "0x" + "12" * 32, "status": "cancelled"},
            "/orders/cancel-batch": {"cancelled": ["0x" + "12" * 32], "errors": []},
            "/orders/cancel-all": {"cancelled_count": 1, "market_ids_affected": ["42"]},
            "/orders/batch-modify": {"placed": ["0x" + "34" * 32], "cancelled": [], "errors": []},
        }
        calls = []

        def fake_request(method: str, url: str, *, params=None, json=None, headers=None, timeout=None):
            path = url.split("api-v2.myriadprotocol.com", 1)[-1]
            calls.append((method, path, params, json, headers))
            return FakeResponse(responses[path])

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict(
            "os.environ",
            {"MYRIAD_API_KEY": "myriad-key", "MYRIAD_API_SECRET": "myriad-secret"},
        ):
            cancelled = adapter.manage_orders(
                "cancel_order",
                order_hash="0x" + "12" * 32,
                instructions=entry,
                confirm_order_management=confirmation,
            )
            batch = adapter.manage_orders(
                "batch_cancel_orders",
                orders=[entry],
                confirm_order_management=confirmation,
            )
            global_cancel = adapter.manage_orders(
                "cancel_all_orders",
                trader=signed_order["trader"],
                timestamp=1_719_835_200,
                signature="0x" + "cd" * 65,
                confirm_global_cancel=global_confirmation,
                confirm_order_management=confirmation,
            )
            modified = adapter.manage_orders(
                "batch_modify_orders",
                modify={"cancel": [entry], "place": [{**entry, "time_in_force": "GTC"}]},
                confirm_order_management=confirmation,
            )

        self.assertEqual(cancelled["response"]["status"], "cancelled")
        self.assertEqual(batch["response"]["cancelled"], ["0x" + "12" * 32])
        self.assertEqual(global_cancel["response"]["cancelled_count"], 1)
        self.assertEqual(modified["request"]["path"], "/orders/batch-modify")
        self.assertEqual(calls[0][0:2], ("DELETE", "/orders/0x" + "12" * 32))
        self.assertEqual(calls[1][0:2], ("POST", "/orders/cancel-batch"))
        self.assertEqual(calls[2][0:2], ("POST", "/orders/cancel-all"))
        self.assertEqual(calls[3][0:2], ("POST", "/orders/batch-modify"))
        for call in calls:
            self.assertEqual(call[4]["x-api-key"], "myriad-key")
            self.assertRegex(call[4]["x-api-signature"], r"^[0-9a-f]{64}$")
        self.assertEqual(adapter.health_check()["order_management_operations"], [
            "cancel_order", "batch_cancel_orders", "cancel_all_orders", "batch_modify_orders"
        ])
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("cancel_order", order_hash="../private", instructions=entry, confirm_order_management=confirmation)
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders("batch_cancel_orders", orders=[entry, entry], confirm_order_management=confirmation)
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_all_orders",
                trader=signed_order["trader"],
                timestamp=1_719_835_200,
                signature="0x" + "cd" * 65,
                confirm_global_cancel="wrong",
                confirm_order_management=confirmation,
            )

    def test_myriad_public_orderbook_trades_are_normalized_and_bounded(self) -> None:
        adapter = MyriadAdapter({"myriad_network_id": 56})
        trades = load_fixture("myriad_markets", "trades")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertTrue(url.endswith("/markets/501/trades"))
            self.assertEqual(params["page"], 1)
            self.assertEqual(params["limit"], 3)
            self.assertEqual(params["outcome"], 1)
            self.assertEqual(params["network_id"], 56)
            return trades

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        result = adapter.list_trades("501:1", limit=3, after=1719835050, before=1719835250)

        self.assertEqual([trade.trade_id for trade in result], ["0xmyriadtradebuy", "0xmyriadtradesell"])
        self.assertEqual([trade.side for trade in result], ["BUY", "SELL"])
        self.assertAlmostEqual(result[0].price, 0.55)
        self.assertAlmostEqual(result[0].size, 2.0)
        self.assertEqual(result[0].timestamp, 1719835200.0)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("501:1", limit=201)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades("501:1", before=100, after=100)

    def test_myriad_public_wallet_events_support_safe_simulation_copy(self) -> None:
        wallet = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
        adapter = MyriadAdapter({"myriad_network_id": 56})
        events = load_fixture("myriad_markets", "user_events")
        portfolio = load_fixture("myriad_markets", "portfolio")
        market_positions = load_fixture("myriad_markets", "market_positions")
        request_limits = []

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith(f"/users/{wallet}/events"):
                self.assertEqual(params["page"], 1)
                request_limits.append(params["limit"])
                self.assertEqual(params["trading_model"], "all")
                self.assertEqual(params["only_relevant"], "true")
                self.assertEqual(params["network_id"], 56)
                return events
            if url.endswith(f"/users/{wallet}/portfolio"):
                self.assertEqual(params["page"], 2)
                self.assertEqual(params["limit"], 10)
                self.assertEqual(params["trading_model"], "all")
                self.assertEqual(params["min_shares"], 1.5)
                self.assertEqual(params["market_slug"], "btc-above-100k-2026")
                self.assertEqual(params["market_id"], 501)
                self.assertEqual(params["network_id"], 56)
                self.assertEqual(params["token_address"], wallet)
                self.assertEqual(params["status"], "ongoing")
                self.assertEqual(params["exclude_history"], True)
                self.assertEqual(params["group_by_event"], True)
                return portfolio
            if url.endswith(f"/users/{wallet}/markets"):
                self.assertEqual(params["state"], "open")
                self.assertEqual(params["topics"], "crypto,macro")
                self.assertEqual(params["market_ids"], "56:501,56:502")
                return market_positions
            self.fail(f"unexpected Myriad account URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        activities = adapter.list_activity(wallet)

        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertEqual(len(activities), 2)
        buy, sell = activities
        self.assertEqual(buy["asset"], "501:1")
        self.assertEqual(buy["side"], "BUY")
        self.assertAlmostEqual(buy["size"], 12.2)
        self.assertAlmostEqual(buy["price"], 0.61)
        self.assertEqual(sell["asset"], "501:2")
        self.assertEqual(sell["side"], "SELL")
        self.assertAlmostEqual(sell["size"], 4.0)
        self.assertAlmostEqual(sell["price"], 0.39)

        recovered = adapter.account_recovery("account_activity", wallet=wallet, limit=10)
        self.assertEqual(
            adapter.health_check()["account_recovery_operations"],
            ["account_activity", "portfolio", "market_positions"],
        )
        self.assertEqual(recovered["source"], "myriad_user_event_feed")
        self.assertEqual(recovered["wallet"], wallet)
        self.assertEqual(recovered["limit"], 10)
        self.assertIs(recovered["raw"], events)
        self.assertEqual(len(recovered["activities"]), 2)
        self.assertEqual(request_limits, [25, 10])

        recovered_portfolio = adapter.account_recovery(
            "portfolio",
            wallet=wallet,
            page=2,
            limit=10,
            min_shares="1.5",
            market_slug="btc-above-100k-2026",
            market_id="501",
            token_address=wallet,
            status="ongoing",
            exclude_history=True,
            group_by_event=True,
        )
        self.assertEqual(
            adapter.health_check()["account_recovery_operations"],
            ["account_activity", "portfolio", "market_positions"],
        )
        self.assertEqual(recovered_portfolio["source"], "myriad_user_portfolio")
        self.assertEqual(recovered_portfolio["positions"][0]["marketId"], 501)
        self.assertIs(recovered_portfolio["raw"], portfolio)

        recovered_markets = adapter.account_recovery(
            "market_positions",
            wallet=wallet,
            topics="crypto,macro",
            market_ids="56:501,56:502",
            state="open",
        )
        self.assertEqual(recovered_markets["source"], "myriad_user_market_positions")
        self.assertEqual(recovered_markets["markets"][0]["state"], "open")
        self.assertIs(recovered_markets["raw"], market_positions)

        result = adapter.copy_trade_from_activity(sell)
        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["request"]["action"], "sell")
        self.assertEqual(result.raw["request"]["shares"], 4.0)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("not-a-wallet")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("unsupported", wallet=wallet)
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("account_activity", wallet=wallet, limit=101)
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("portfolio", wallet=wallet, trading_model="invalid")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("market_positions", wallet=wallet, topics="bad/topic")

    def test_myriad_position_intents_validate_and_return_unsigned_calldata(self) -> None:
        adapter = MyriadAdapter({"myriad_network_id": 56})
        response = load_fixture("myriad_markets", "position_intent")
        calls = []

        def fake_request(method: str, url: str, **kwargs):
            calls.append((method, url, kwargs))
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["json"]["network_id"], 56)
            self.assertNotIn("Authorization", kwargs["headers"])
            return FakeResponse(response)

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        split = adapter.position_intent("split", market_id="501", amount="1000000000000000000")
        self.assertEqual(split["endpoint"], "/positions/split")
        self.assertTrue(split["intent_only"])
        self.assertFalse(split["signed"])
        self.assertEqual(split["transaction"]["value"], "0")
        self.assertEqual(split["request"]["amount"], "1000000000000000000")

        neg = adapter.position_intent(
            "neg-risk-split",
            event_id="0x" + "ab" * 32,
            outcome_index=2,
            amount="7",
        )
        self.assertEqual(neg["endpoint"], "/positions/neg-risk/split")
        self.assertEqual(neg["request"]["outcome_index"], 2)
        self.assertNotIn("market_id", neg["request"])
        self.assertEqual(len(calls), 2)

        with self.assertRaises(MarketConfigurationError):
            adapter.position_intent("split", market_id=501, amount="0")
        with self.assertRaises(MarketConfigurationError):
            adapter.position_intent("neg_risk_merge", event_id="0x" + "ab" * 32)
        with self.assertRaises(MarketConfigurationError):
            adapter.position_intent("neg_risk_merge", market_id=501, event_id="0x00", outcome_index=0)

    def test_opinion_adapter_requires_key_and_maps_market_data(self) -> None:
        adapter = OpinionAdapter()
        markets = load_fixture("opinion_labs", "markets")
        market = load_fixture("opinion_labs", "market")
        price_payload = load_fixture("opinion_labs", "price")
        orderbook = load_fixture("opinion_labs", "orderbook")
        price_history = load_fixture("opinion_labs", "price_history")
        trades = load_fixture("opinion_labs", "trades")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers["apikey"], "opinion-key")
            if url.endswith("/market"):
                return markets
            if url.endswith("/market/77"):
                return market
            if url.endswith("/token/latest-price"):
                return price_payload
            if url.endswith("/token/orderbook"):
                return orderbook
            if url.endswith("/token/price-history"):
                self.assertEqual(params["token_id"], "0xyes")
                self.assertEqual(params["interval"], "1d")
                self.assertEqual(params["start_at"], 1733184000)
                self.assertEqual(params["end_at"], 1733356800)
                return price_history
            if url.endswith("/trade/user/0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"):
                if params and "marketId" in params:
                    self.assertEqual(params["marketId"], 77)
                return trades
            raise AssertionError(f"unexpected Opinion URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        with self.assertRaises(MarketConfigurationError):
            adapter.list_events()

        with patch.dict(
            "os.environ",
            {
                "OPINION_API_KEY": "opinion-key",
                "OPINION_WALLET_ADDRESS": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ):
            events = adapter.list_events("ETH")
            contracts = adapter.list_contracts("77")
            price = adapter.get_price("77:YES:0xyes")
            book = adapter.get_orderbook("77:YES:0xyes")
            candles = adapter.list_candles(
                "77:YES:0xyes",
                resolution="1d",
                from_timestamp=1733184000,
                to_timestamp=1733356800,
            )
            trade_history = adapter.list_trades(
                "77:YES:0xyes",
                limit=2,
                after=1733312000,
                before=1733313000,
            )
            paper = adapter.place_paper_order(PaperOrderRequest("opinion_labs", "77:YES:0xyes", "SELL", 4, 0.64))
            activity = adapter.list_activity("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
            copied = adapter.copy_trade_from_activity(activity[0])

        self.assertEqual(events[0].event_id, "77")
        self.assertEqual([contract.contract_id for contract in contracts], ["77:YES:0xyes", "77:NO:0xno"])
        self.assertEqual(price.last, 0.65)
        self.assertEqual([level.price for level in book.bids], [0.64, 0.62])
        self.assertEqual([candle.timestamp for candle in candles], [1733184000.0, 1733270400.0, 1733356800.0])
        self.assertEqual([candle.close for candle in candles], [0.58, 0.62, 0.65])
        self.assertTrue(all(candle.volume is None for candle in candles))
        self.assertEqual([trade.trade_id for trade in trade_history], ["0xopiniontrade2"])
        self.assertEqual([trade.side for trade in trade_history], ["BUY"])
        self.assertEqual([trade.price for trade in trade_history], [0.65])
        self.assertEqual([trade.size for trade in trade_history], [4.0])
        self.assertEqual([trade.timestamp for trade in trade_history], [1733312400.0])
        self.assertTrue(paper.accepted)
        self.assertEqual(len(activity), 2)
        self.assertEqual(activity[0]["asset"], "77:YES:0xyes")
        self.assertEqual(activity[0]["side"], "BUY")
        self.assertEqual(activity[0]["timestamp"], 1733312400)
        self.assertTrue(copied.accepted)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("not-a-wallet")

        with patch.dict("os.environ", {"OPINION_API_KEY": "opinion-key"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_candles("77:YES:0xyes", resolution="30m")
            with self.assertRaises(MarketConfigurationError):
                adapter.list_candles(
                    "77:YES:0xyes",
                    from_timestamp=1733356800,
                    to_timestamp=1733184000,
                )
            with self.assertRaises(MarketConfigurationError):
                adapter.list_trades("77:YES:0xyes", limit=21)
            with self.assertRaises(MarketConfigurationError):
                adapter.list_trades("77:YES:0xyes", after=1733313000, before=1733312000)

    def test_opinion_account_recovery_reads_are_authenticated_and_bounded(self) -> None:
        wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        adapter = OpinionAdapter(
            {
                "opinion_api_key": "opinion-key",
                "opinion_account_wallet": wallet,
            }
        )
        orders = load_fixture("opinion_labs", "orders")
        order_detail = load_fixture("opinion_labs", "order_detail")
        positions = load_fixture("opinion_labs", "positions")
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            self.assertEqual(headers, {"apikey": "opinion-key"})
            if url.endswith("/order"):
                self.assertEqual(params, {"page": 2, "limit": 20, "marketId": 77, "chainId": "56", "status": "1,2"})
                return orders
            if url.endswith("/order/order-1"):
                self.assertIsNone(params)
                return order_detail
            if url.endswith(f"/positions/user/{wallet}"):
                self.assertEqual(params, {"page": 1, "limit": 10, "marketId": 77, "chainId": "56"})
                return positions
            raise AssertionError(f"unexpected Opinion account URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        history = adapter.account_recovery(
            "order_history",
            page=2,
            limit=20,
            market_id="77",
            chain_id="56",
            status="1,2",
        )
        detail = adapter.account_recovery("order_detail", order_id="order-1")
        account_positions = adapter.account_recovery(
            "positions", page=1, limit=10, market_id="77", chain_id="56"
        )
        self.assertEqual(history["result"]["list"][0]["orderId"], "order-1")
        self.assertEqual(detail["result"]["orderData"]["orderId"], "order-1")
        self.assertEqual(account_positions["result"]["list"][0]["tokenId"], "0xyes")
        self.assertEqual(len(calls), 3)

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("order_history", status="1,6")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("order_detail", order_id="../outside")
        with self.assertRaises(MarketConfigurationError):
            OpinionAdapter({"opinion_api_key": "opinion-key"}).account_recovery("positions")

    def test_opinion_guarded_clob_orders_build_signed_limit_and_market_requests(self) -> None:
        adapter = OpinionAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": 25,
                "opinion_live_check_approval": False,
            }
        )
        submitted = []

        class FakeClient:
            def place_order(self, payload, *, check_approval=False):
                submitted.append((payload, check_approval))
                return {"order_id": "opinion-order-1", "status": "submitted"}

        def fake_builder(**kwargs):
            return kwargs

        with patch.object(adapter, "_create_clob_client", return_value=FakeClient()), patch.object(
            OpinionAdapter, "_build_sdk_order", side_effect=fake_builder
        ):
            limit = adapter.place_live_order(
                PaperOrderRequest("opinion_labs", "77:YES:0xyes", "BUY", 4, 0.64)
            )
            market = adapter.place_live_order(
                PaperOrderRequest(
                    "opinion_labs",
                    "77:NO:0xno",
                    "SELL",
                    3,
                    None,
                    {"order_type": "market", "maker_amount_in_base_token": "3"},
                )
            )

        self.assertTrue(limit["live"])
        self.assertEqual(limit["request"]["marketId"], 77)
        self.assertEqual(limit["request"]["tokenId"], "0xyes")
        self.assertEqual(limit["request"]["price"], "0.64")
        self.assertEqual(limit["request"]["makerAmountInQuoteToken"], "4")
        self.assertIsNone(limit["request"]["makerAmountInBaseToken"])
        self.assertEqual(limit["response"]["order_id"], "opinion-order-1")
        self.assertEqual(market["order_type"], "market")
        self.assertEqual(market["request"]["price"], "0")
        self.assertEqual(market["request"]["makerAmountInBaseToken"], "3")
        self.assertEqual(len(submitted), 2)
        self.assertFalse(submitted[0][1])

        with patch.object(adapter, "_create_clob_client", return_value=FakeClient()), patch.object(
            OpinionAdapter, "_build_sdk_order", side_effect=fake_builder
        ):
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(
                    PaperOrderRequest("opinion_labs", "77:YES:0xyes", "BUY", 1, 0.005)
                )
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(
                    PaperOrderRequest(
                        "opinion_labs",
                        "77:YES:0xyes",
                        "BUY",
                        1,
                        0.5,
                        {
                            "maker_amount_in_quote_token": "1",
                            "maker_amount_in_base_token": "1",
                        },
                    )
                )

    def test_opinion_guarded_order_management_uses_documented_sdk_methods(self) -> None:
        adapter = OpinionAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "opinion_order_management_enabled": True,
            }
        )
        calls = []

        class FakeClient:
            def cancel_order(self, order_id):
                calls.append(("cancel_order", order_id))
                return {"orderId": order_id, "status": "cancelled"}

            def cancel_orders_batch(self, order_ids):
                calls.append(("cancel_orders_batch", list(order_ids)))
                return [{"orderId": order_id, "status": "cancelled"} for order_id in order_ids]

            def cancel_all_orders(self, *, market_id=None, side=None):
                calls.append(("cancel_all_orders", market_id, side))
                return {"cancelled": 2}

        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        with patch.object(adapter, "_create_clob_client", return_value=FakeClient()):
            single = adapter.manage_orders(
                "cancel_order",
                order_id="order-1",
                confirm_order_management=confirmation,
            )
            batch = adapter.manage_orders(
                "batch_cancel_orders",
                orders=["order-1", "order-2"],
                confirm_order_management=confirmation,
            )
            global_cancel = adapter.manage_orders(
                "cancel_all_orders",
                confirm_order_management=confirmation,
                confirm_global_cancel="CANCEL ALL OPINION ORDERS",
            )

        self.assertEqual(single["request"], {"orderId": "order-1"})
        self.assertEqual(batch["request"], {"orderIds": ["order-1", "order-2"]})
        self.assertEqual(global_cancel["request"], {})
        self.assertEqual(calls, [
            ("cancel_order", "order-1"),
            ("cancel_orders_batch", ["order-1", "order-2"]),
            ("cancel_all_orders", None, None),
        ])
        self.assertEqual(
            adapter.health_check()["order_management_operations"],
            ["cancel_order", "batch_cancel_orders", "cancel_all_orders"],
        )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_order",
                order_id="../outside",
                confirm_order_management=confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "batch_cancel_orders",
                orders=["order-1", "order-1"],
                confirm_order_management=confirmation,
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_all_orders",
                confirm_order_management=confirmation,
                confirm_global_cancel="wrong",
            )

    def test_predict_fun_adapter_maps_markets_orderbooks_and_no_prices(self) -> None:
        adapter = PredictFunAdapter()
        markets = load_fixture("predict_fun", "markets")
        market = load_fixture("predict_fun", "market")
        orderbook = load_fixture("predict_fun", "orderbook")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers["x-api-key"], "predict-key")
            if url.endswith("/markets"):
                return markets
            if url.endswith("/markets/9001"):
                return market
            if url.endswith("/markets/9001/orderbook"):
                return orderbook
            raise AssertionError(f"unexpected Predict.fun URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key"}):
            events = adapter.list_events("SOL")
            contracts = adapter.list_contracts("9001")
            yes_book = adapter.get_orderbook("9001:YES")
            no_book = adapter.get_orderbook("9001:NO")
            price = adapter.get_price("9001:YES")
            paper = adapter.place_paper_order(PaperOrderRequest("predict_fun", "9001:NO", "BUY", 5, 0.44))
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(PaperOrderRequest("predict_fun", "9001:YES", "BUY", 5, 0.56))

        self.assertEqual(events[0].event_id, "9001")
        self.assertEqual([contract.contract_id for contract in contracts], ["9001:YES", "9001:NO"])
        self.assertEqual([level.price for level in yes_book.bids], [0.56, 0.54])
        self.assertEqual([level.price for level in no_book.bids], [0.42, 0.4])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.57)
        self.assertTrue(paper.accepted)

        live_adapter = PredictFunAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append((method, url, json, headers, timeout))
            return FakeResponse(load_fixture("predict_fun", "order_response"))

        market["data"]["outcomes"][0]["onChainId"] = "111"
        market["data"]["outcomes"][1]["onChainId"] = "222"
        live_adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key"}):
            result = live_adapter.place_live_order(
                PaperOrderRequest(
                    "predict_fun",
                    "9001:YES",
                    "BUY",
                    5,
                    0.56,
                    {
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
                        }
                    },
                )
            )

        self.assertEqual(result["response"]["data"]["orderId"], "pf_order_123")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders"))
        self.assertEqual(calls[0][2]["data"]["pricePerShare"], "0.56")
        self.assertEqual(calls[0][3]["x-api-key"], "predict-key")

    def test_predict_fun_account_history_positions_timeseries_and_guarded_removal(self) -> None:
        adapter = PredictFunAdapter()
        fixtures = {
            name: load_fixture("predict_fun", name)
            for name in ("account", "orders", "order_detail", "activity", "positions", "timeseries", "matches")
        }
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, params, headers))
            self.assertEqual(headers["x-api-key"], "predict-key")
            if url.endswith("/account"):
                self.assertEqual(headers["Authorization"], "Bearer jwt-token")
                return fixtures["account"]
            if url.endswith("/orders/matches"):
                self.assertEqual(params, {"first": 25, "marketId": 9001})
                return fixtures["matches"]
            if url.endswith("/orders"):
                self.assertEqual(params, {"first": 25, "after": "next", "status": "OPEN", "marketId": 9001})
                self.assertEqual(headers["Authorization"], "Bearer jwt-token")
                return fixtures["orders"]
            if url.endswith("/orders/0xpredictorder"):
                return fixtures["order_detail"]
            if url.endswith("/account/activity"):
                self.assertEqual(params, {"first": 10, "after": "next-activity", "eventTypes": "ORDER_MATCHED"})
                return fixtures["activity"]
            if url.endswith("/positions"):
                self.assertEqual(params, {"first": 50, "marketId": 9001, "isResolved": "false", "sort": "VALUE_DESC"})
                return fixtures["positions"]
            if "/positions/0x1111111111111111111111111111111111111111" in url:
                return fixtures["positions"]
            if url.endswith("/markets/9001/timeseries"):
                self.assertEqual(params, {"resolution": "1h"})
                return fixtures["timeseries"]
            raise AssertionError(f"unexpected Predict.fun URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key", "PREDICT_FUN_JWT": "jwt-token"}):
            account = adapter.account_recovery("account")
            orders = adapter.account_recovery("active_orders", limit=25, cursor="next", status="OPEN", market_id="9001")
            detail = adapter.account_recovery("order_detail", order_id="0xpredictorder")
            activity = adapter.account_recovery("account_activity", limit=10, cursor="next-activity", event_types="ORDER_MATCHED")
            positions = adapter.account_recovery("positions", market_id="9001", is_resolved=False, sort="VALUE_DESC")
            public_positions = adapter.account_recovery(
                "positions_by_address",
                address="0x1111111111111111111111111111111111111111",
            )
            yes_trades = adapter.list_trades("9001:YES", limit=25, after=1733312000, before=1733317000)
            no_trades = adapter.list_trades("9001:NO", limit=25, after=1733312000, before=1733317000)
            candles = adapter.list_candles("9001:YES", from_timestamp=1733312000, to_timestamp=1733317000)

        self.assertEqual(account["data"]["address"], "0x1111111111111111111111111111111111111111")
        self.assertEqual(orders["data"][0]["id"], "pf_order_123")
        self.assertEqual(detail["data"]["status"], "OPEN")
        self.assertEqual(activity["data"][0]["eventName"], "ORDER_MATCHED")
        self.assertEqual(positions["data"][0]["outcome"], "YES")
        self.assertEqual(public_positions["data"][0]["id"], "position-1")
        self.assertEqual([trade.trade_id for trade in yes_trades], ["0xpredictmatchbuy"])
        self.assertEqual(yes_trades[0].side, "BUY")
        self.assertEqual(yes_trades[0].price, 0.48)
        self.assertEqual(yes_trades[0].size, 5.0)
        self.assertEqual(yes_trades[0].timestamp, 1733312400.0)
        self.assertEqual([trade.trade_id for trade in no_trades], ["0xpredictmatchsell"])
        self.assertEqual(no_trades[0].side, "SELL")
        self.assertEqual([c.close for c in candles], [0.54, 0.57])
        self.assertEqual(candles[0].timestamp, 1733312400.0)

        live = PredictFunAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "predict_fun_order_management_enabled": True,
            }
        )
        removal_calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            removal_calls.append((method, url, json, headers))
            return FakeResponse(load_fixture("predict_fun", "remove_response"))

        live.runtime.session.request = fake_request  # type: ignore[method-assign]
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key", "PREDICT_FUN_JWT": "jwt-token"}):
            removed = live.manage_orders(
                "remove_orders",
                orders=["pf_order_123"],
                confirm_order_management=confirmation,
            )
            removed_by_hash = live.manage_orders(
                "remove_orders_by_hash",
                orders=["0xpredictorder"],
                confirm_order_management=confirmation,
            )

        self.assertFalse(removed["on_chain_cancellation"])
        self.assertEqual(removed["response"]["removed"], ["pf_order_123"])
        self.assertEqual(removed_by_hash["operation"], "remove_orders_by_hash")
        self.assertTrue(removal_calls[0][1].endswith("/v1/orders/remove"))
        self.assertTrue(removal_calls[1][1].endswith("/orders/remove-by-hash"))
        self.assertEqual(removal_calls[0][3]["Authorization"], "Bearer jwt-token")

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("order_detail", order_id="../outside")
        with self.assertRaises(MarketConfigurationError):
            live.manage_orders(
                "remove_orders",
                orders=["pf_order_123"],
                confirm_order_management="wrong",
            )

    def test_predict_fun_authenticated_activity_supports_account_copy_preview(self) -> None:
        adapter = PredictFunAdapter()
        account = load_fixture("predict_fun", "account")
        activity = load_fixture("predict_fun", "activity")
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, params, headers))
            self.assertEqual(headers["x-api-key"], "predict-key")
            self.assertEqual(headers["Authorization"], "Bearer jwt-token")
            if url.endswith("/account"):
                return account
            if url.endswith("/account/activity"):
                self.assertEqual(params, {"first": 10})
                return activity
            raise AssertionError(f"unexpected Predict.fun URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        wallet = "0x1111111111111111111111111111111111111111"
        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key", "PREDICT_FUN_JWT": "jwt-token"}):
            rows = adapter.list_activity(wallet, limit=10)
            result = adapter.copy_trade_from_activity(rows[0])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proxyWallet"], wallet.lower())
        self.assertEqual(rows[0]["asset"], "9001:YES")
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["size"], 5.0)
        self.assertEqual(rows[0]["price"], 0.48)
        self.assertEqual(rows[0]["transactionHash"], "0xpredictactivitybuy")
        self.assertTrue(result.accepted)
        self.assertEqual(len(calls), 2)

        with patch.dict("os.environ", {"PREDICT_FUN_API_KEY": "predict-key", "PREDICT_FUN_JWT": "jwt-token"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_activity("0x2222222222222222222222222222222222222222")

    def test_xo_adapter_uses_hmac_headers_and_keeps_live_orders_guarded(self) -> None:
        adapter = XOMarketAdapter()
        markets = load_fixture("xo_market", "markets")
        market = load_fixture("xo_market", "market")
        orderbook = load_fixture("xo_market", "orderbook")
        trades = load_fixture("xo_market", "trades")
        candles = load_fixture("xo_market", "candles")
        account = load_fixture("xo_market", "account")
        positions = load_fixture("xo_market", "positions")
        orders = load_fixture("xo_market", "orders")
        account_trades = load_fixture("xo_market", "account_trades")
        settlement = load_fixture("xo_market", "settlement")
        settlement_history = load_fixture("xo_market", "settlement_history")
        audit_logs = load_fixture("xo_market", "audit_logs")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers["XO-API-KEY"], "xo-key")
            self.assertTrue(headers["XO-SIGNATURE"])
            if url.endswith("/markets"):
                return markets
            if url.endswith("/markets/us-election-2028"):
                return market
            if url.endswith("/markets/us-election-2028/outcomes/vance/orderbook"):
                return orderbook
            if url.endswith("/markets/us-election-2028/trades"):
                self.assertIsNone(params)
                return trades
            if url.endswith("/markets/us-election-2028/candles"):
                self.assertEqual(
                    params,
                    {
                        "outcome_id": "vance",
                        "interval": "1h",
                        "start_time": "2024-12-01T09:00:00.000Z",
                        "end_time": "2024-12-01T10:00:00.000Z",
                        "limit": 1000,
                    },
                )
                return candles
            if url.endswith("/account"):
                self.assertIsNone(params)
                return account
            if url.endswith("/positions"):
                self.assertIsNone(params)
                return positions
            if url.endswith("/orders"):
                self.assertIsNone(params)
                return orders
            if url.endswith("/trades"):
                self.assertEqual(
                    params,
                    {
                        "market_id": "us-election-2028",
                        "outcome_id": "vance",
                        "start_time": "2024-12-01T09:15:00.000Z",
                        "end_time": "2024-12-01T09:30:00.000Z",
                        "limit": 2,
                    },
                )
                return account_trades
            if url.endswith("/markets/us-election-2028/settlement"):
                self.assertIsNone(params)
                return settlement
            if url.endswith("/markets/us-election-2028/settlement/history"):
                self.assertEqual(params, {"limit": 5, "cursor": "next-page"})
                return settlement_history
            if url.endswith("/audit/logs"):
                self.assertEqual(
                    params,
                    {
                        "event_type": "order_filled",
                        "start_time": "2024-12-01T09:15:00.000Z",
                        "end_time": "2024-12-01T09:30:00.000Z",
                        "limit": 2,
                    },
                )
                return audit_logs
            raise AssertionError(f"unexpected XO URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        order = PaperOrderRequest("xo_market", "us-election-2028:vance", "BUY", 25, 0.35)

        with patch.dict("os.environ", {"XO_API_KEY": "xo-key", "XO_API_SECRET": "xo-secret"}):
            events = adapter.list_events("election")
            contracts = adapter.list_contracts("us-election-2028")
            price = adapter.get_price("us-election-2028:vance")
            trade_history = adapter.list_trades(
                order.contract_id,
                limit=2,
                after=1733044500,
                before=1733045400,
            )
            candle_history = adapter.list_candles(
                order.contract_id,
                resolution="1h",
                from_timestamp=1733043600,
                to_timestamp=1733047200,
            )
            account_payload = adapter.account_recovery("account")
            positions_payload = adapter.account_recovery("positions")
            orders_payload = adapter.account_recovery("orders")
            recovered_trades = adapter.account_recovery(
                "trades",
                market_id="us-election-2028",
                outcome_id="vance",
                start_time=1733044500,
                end_time=1733045400,
                limit=2,
            )
            copy_preview = adapter.copy_trade_from_activity(account_trades["trades"][0])
            settlement_payload = adapter.account_recovery("settlement", market_id="us-election-2028")
            settlement_history_payload = adapter.account_recovery(
                "settlement_history",
                market_id="us-election-2028",
                limit=5,
                cursor="next-page",
            )
            audit_payload = adapter.account_recovery(
                "audit_logs",
                event_type="order_filled",
                start_time=1733044500,
                end_time=1733045400,
                limit=2,
            )
            paper = adapter.place_paper_order(order)
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(order)

        self.assertEqual(events[0].event_id, "us-election-2028")
        self.assertEqual([contract.contract_id for contract in contracts], ["us-election-2028:vance", "us-election-2028:newsom"])
        self.assertAlmostEqual(price.midpoint or 0.0, 0.35)
        self.assertEqual([trade.trade_id for trade in trade_history], ["trd_8a7b6c5d"])
        self.assertEqual(trade_history[0].side, "BUY")
        self.assertAlmostEqual(trade_history[0].price, 0.35)
        self.assertAlmostEqual(trade_history[0].size, 14285.71)
        self.assertAlmostEqual(trade_history[0].timestamp or 0.0, 1733044500.456, places=3)
        self.assertEqual(len(candle_history), 1)
        self.assertEqual(candle_history[0].contract_id, order.contract_id)
        self.assertAlmostEqual(candle_history[0].open, 0.34)
        self.assertAlmostEqual(candle_history[0].close, 0.35)
        self.assertAlmostEqual(candle_history[0].volume or 0.0, 125000.0)
        self.assertEqual(account_payload["id"], "acc_8a9b2c3d")
        self.assertEqual(positions_payload["positions"][0]["outcome_id"], "vance")
        self.assertEqual(orders_payload["orders"][0]["status"], "filled")
        self.assertEqual(recovered_trades["trades"][0]["trade_id"], "trd_8a7b6c5d")
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "us-election-2028:vance")
        self.assertEqual(copy_preview.raw["source"], "xo_account_trades")
        self.assertEqual(settlement_payload["status"], "resolved")
        self.assertEqual(settlement_history_payload["settlements"][0]["status"], "resolved")
        self.assertEqual(audit_payload["events"][0]["event_type"], "order_filled")
        self.assertEqual(paper.raw["request"]["amount_usd"], 25.0)

        live_adapter = XOMarketAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request(method: str, url: str, *, params=None, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers, timeout))
            return FakeResponse(load_fixture("xo_market", "order_response"))

        live_adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"XO_API_KEY": "xo-key", "XO_API_SECRET": "xo-secret"}):
            result = live_adapter.place_live_order(order)

        self.assertEqual(result["response"]["id"], "ord_123")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/orders"))
        self.assertIn('"market_id":"us-election-2028"', calls[0][2])
        self.assertEqual(calls[0][3]["XO-API-KEY"], "xo-key")

        management = XOMarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "xo_order_management_enabled": True,
            }
        )
        management_calls = []

        def fake_cancel_request(method: str, url: str, *, params=None, data=None, headers=None, timeout=None):
            management_calls.append((method, url, params, data, headers, timeout))
            return FakeResponse(load_fixture("xo_market", "cancel_response"))

        management.runtime.session.request = fake_cancel_request  # type: ignore[method-assign]
        with patch.dict("os.environ", {"XO_API_KEY": "xo-key", "XO_API_SECRET": "xo-secret"}):
            cancelled = management.manage_orders(
                "cancel_order",
                order_id="ord_992837",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

        self.assertEqual(cancelled["response"]["status"], "cancelled")
        self.assertEqual(management_calls[0][0], "DELETE")
        self.assertTrue(management_calls[0][1].endswith("/orders/ord_992837"))
        self.assertEqual(management_calls[0][3], "")
        self.assertEqual(management_calls[0][4]["XO-API-KEY"], "xo-key")
        with self.assertRaises(MarketConfigurationError):
            management.manage_orders(
                "cancel_order",
                order_id="../outside",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

    def test_betfair_adapter_maps_market_catalogue_and_best_offer_books(self) -> None:
        adapter = BetfairExchangeAdapter()
        catalogue = load_fixture("betfair_exchange", "market_catalogue")["result"]
        market_book = load_fixture("betfair_exchange", "market_book")["result"]
        current_orders = load_fixture("betfair_exchange", "current_orders")["result"]
        cleared_orders = load_fixture("betfair_exchange", "cleared_orders")["result"]
        account_funds = load_fixture("betfair_exchange", "account_funds")["result"]
        account_details = load_fixture("betfair_exchange", "account_details")["result"]
        account_statement = load_fixture("betfair_exchange", "account_statement")["result"]
        currency_rates = load_fixture("betfair_exchange", "currency_rates")["result"]
        place_response = load_fixture("betfair_exchange", "place_order_response")

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            self.assertEqual(headers["X-Application"], "betfair-app")
            self.assertEqual(headers["X-Authentication"], "betfair-session")
            if json["method"].endswith("listMarketCatalogue"):
                return FakeResponse({"jsonrpc": "2.0", "result": catalogue, "id": 1})
            if json["method"].endswith("listMarketBook"):
                return FakeResponse({"jsonrpc": "2.0", "result": market_book, "id": 1})
            if json["method"].endswith("listCurrentOrders"):
                self.assertEqual(json["params"]["marketIds"], ["1.234"])
                self.assertEqual(json["params"]["orderBy"], "BY_MATCH_TIME")
                return FakeResponse({"jsonrpc": "2.0", "result": current_orders, "id": 1})
            if json["method"].endswith("listClearedOrders"):
                self.assertEqual(json["params"]["marketIds"], ["1.234"])
                self.assertEqual(json["params"]["betStatus"], "SETTLED")
                return FakeResponse({"jsonrpc": "2.0", "result": cleared_orders, "id": 1})
            if json["method"].endswith("getAccountFunds"):
                self.assertTrue(url.endswith("/exchange/account/json-rpc/v1"))
                self.assertEqual(json["params"]["wallet"], "UK")
                return FakeResponse({"jsonrpc": "2.0", "result": account_funds, "id": 1})
            if json["method"].endswith("getAccountDetails"):
                self.assertTrue(url.endswith("/exchange/account/json-rpc/v1"))
                self.assertEqual(json["params"], {})
                return FakeResponse({"jsonrpc": "2.0", "result": account_details, "id": 1})
            if json["method"].endswith("getAccountStatement"):
                self.assertTrue(url.endswith("/exchange/account/json-rpc/v1"))
                self.assertEqual(json["params"]["locale"], "en")
                self.assertEqual(json["params"]["fromRecord"], 3)
                self.assertEqual(json["params"]["recordCount"], 10)
                self.assertEqual(json["params"]["wallet"], "UK")
                self.assertEqual(
                    json["params"]["itemDateRange"],
                    {"from": "2026-06-01T00:00:00.000Z", "to": "2026-06-02T00:00:00.000Z"},
                )
                return FakeResponse({"jsonrpc": "2.0", "result": account_statement, "id": 1})
            if json["method"].endswith("listCurrencyRates"):
                self.assertTrue(url.endswith("/exchange/account/json-rpc/v1"))
                self.assertEqual(json["params"], {"fromCurrency": "GBP"})
                return FakeResponse({"jsonrpc": "2.0", "result": currency_rates, "id": 1})
            if json["method"].endswith("placeOrders"):
                return FakeResponse({"jsonrpc": "2.0", "result": place_response, "id": 1})
            raise AssertionError(f"unexpected Betfair method: {json['method']}")

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]

        with patch.dict(
            "os.environ",
            {"BETFAIR_APP_KEY": "betfair-app", "BETFAIR_SESSION_TOKEN": "betfair-session"},
        ):
            events = adapter.list_events("Team")
            contracts = adapter.list_contracts("1.234")
            book = adapter.get_orderbook("1.234:101")
            price = adapter.get_price("1.234:101")
            trades = adapter.list_trades("1.234:101", limit=2, after=1780344000, before=1780344200)
            candles = adapter.list_candles(
                "1.234:101",
                resolution="1h",
                from_timestamp=1780344000,
                to_timestamp=1780344200,
            )
            active = adapter.account_recovery(
                "active_orders",
                contract_id="1.234:101",
                status="EXECUTION_COMPLETE",
                limit=10,
                offset=1,
            )
            settled = adapter.account_recovery(
                "cleared_orders",
                market_id="1.234",
                limit=10,
                offset=2,
                from_timestamp=1780308000,
                to_timestamp=1780394400,
            )
            funds = adapter.account_recovery("funds", wallet="UK")
            details = adapter.account_recovery("account")
            statement = adapter.account_recovery(
                "statement",
                locale="en",
                limit=10,
                offset=3,
                include_item=True,
                wallet="UK",
                from_timestamp=1780272000,
                to_timestamp=1780358400,
            )
            rates = adapter.account_recovery("currency_rates", from_currency="GBP")
            paper = adapter.place_paper_order(PaperOrderRequest("betfair_exchange", "1.234:101", "BACK", 10, 0.5))
            with self.assertRaises(MarketConfigurationError):
                adapter.place_live_order(PaperOrderRequest("betfair_exchange", "1.234:101", "BACK", 10, 0.5))

        self.assertEqual(events[0].event_id, "1.234")
        self.assertEqual([contract.contract_id for contract in contracts], ["1.234:101", "1.234:102"])
        self.assertEqual([round(level.price, 4) for level in book.bids], [0.5, 0.4545])
        self.assertEqual([round(level.price, 4) for level in book.asks], [0.5556, 0.5882])
        self.assertAlmostEqual(price.midpoint or 0.0, (0.5 + (1 / 1.8)) / 2)
        self.assertEqual([trade.trade_id for trade in trades], ["bet_matched_101"])
        self.assertEqual([trade.side for trade in trades], ["BUY"])
        self.assertEqual([trade.price for trade in trades], [0.5])
        self.assertEqual([trade.size for trade in trades], [4.0])
        self.assertEqual([trade.timestamp for trade in trades], [1780344003.0])
        copy_preview = adapter.copy_trade_from_activity(current_orders["currentOrders"][0])
        self.assertTrue(copy_preview.accepted)
        self.assertEqual(copy_preview.contract_id, "1.234:101")
        self.assertEqual(copy_preview.raw["source"], "betfair_authenticated_current_orders")
        self.assertAlmostEqual(copy_preview.average_price or 0.0, 0.5)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp, 1780344000.0)
        self.assertAlmostEqual(candles[0].open, 0.5)
        self.assertAlmostEqual(candles[0].volume or 0, 4.0)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(candles[0].raw["source"], "betfair_matched_account_orders")
        self.assertEqual([row["betId"] for row in active["currentOrders"]], ["bet_matched_101"])
        self.assertEqual(set(settled), {"clearedOrders", "moreAvailable"})
        self.assertEqual(settled["clearedOrders"][0]["betId"], "bet-1")
        self.assertEqual(funds["availableToBetBalance"], 125.5)
        self.assertEqual(details["currencyCode"], "GBP")
        self.assertEqual(statement["accountStatement"][0]["refId"], "bet-1")
        self.assertEqual(rates[0]["currencyCode"], "EUR")
        self.assertTrue(paper.accepted)

        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**current_orders["currentOrders"][0], "side": "UNKNOWN"})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({**current_orders["currentOrders"][0], "sizeMatched": 0})

        with patch.dict(
            "os.environ",
            {"BETFAIR_APP_KEY": "betfair-app", "BETFAIR_SESSION_TOKEN": "betfair-session"},
        ):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_trades("1.234:101", limit=1001)
            with self.assertRaises(MarketConfigurationError):
                adapter.list_trades("1.234:101", after=1780344200, before=1780344000)
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("cleared_orders", bet_status="OPEN")
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("cleared_orders", market_id="../outside")
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("active_orders", status="OPEN")
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("funds", wallet="UK-1")
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("statement", locale="en/../", limit=10)
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("statement", from_timestamp=1780358400, to_timestamp=1780272000)
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("currency_rates", from_currency="GBP1")

        live_adapter = BetfairExchangeAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_live_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            calls.append((method, url, json, headers, timeout))
            return FakeResponse({"jsonrpc": "2.0", "result": place_response, "id": 1})

        live_adapter.runtime.session.request = fake_live_request  # type: ignore[method-assign]
        with patch.dict(
            "os.environ",
            {"BETFAIR_APP_KEY": "betfair-app", "BETFAIR_SESSION_TOKEN": "betfair-session"},
        ):
            result = live_adapter.place_live_order(
                PaperOrderRequest("betfair_exchange", "1.234:101", "BACK", 10, 0.5, {"customer_ref": "client-1"})
            )

        self.assertEqual(result["response"]["status"], "SUCCESS")
        self.assertEqual(calls[0][2]["method"], "SportsAPING/v1.0/placeOrders")
        instruction = calls[0][2]["params"]["instructions"][0]
        self.assertEqual(instruction["side"], "BACK")
        self.assertEqual(instruction["limitOrder"]["price"], "2.0")

    def test_betfair_order_management_uses_guarded_official_mutation_endpoints(self) -> None:
        adapter = BetfairExchangeAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "betfair_order_management_enabled": True,
            }
        )
        responses = {
            "cancelOrders": load_fixture("betfair_exchange", "cancel_orders_response"),
            "updateOrders": load_fixture("betfair_exchange", "update_orders_response"),
            "replaceOrders": load_fixture("betfair_exchange", "replace_orders_response"),
        }
        calls = []

        def fake_request(method: str, url: str, *, json=None, headers=None, timeout=None):
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/exchange/betting/json-rpc/v1"))
            self.assertEqual(headers["X-Application"], "betfair-app")
            self.assertEqual(headers["X-Authentication"], "betfair-session")
            calls.append(json)
            for endpoint, response in responses.items():
                if json["method"].endswith(endpoint):
                    return FakeResponse(response)
            raise AssertionError(f"unexpected Betfair method: {json['method']}")

        adapter.runtime.session.request = fake_request  # type: ignore[method-assign]
        with patch.dict(
            "os.environ",
            {"BETFAIR_APP_KEY": "betfair-app", "BETFAIR_SESSION_TOKEN": "betfair-session"},
        ):
            cancelled = adapter.manage_orders(
                "cancel_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1", "size_reduction": 1.25}],
                customer_ref="cancel-1",
            )
            updated = adapter.manage_orders(
                "update_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1", "new_persistence_type": "persist"}],
            )
            replaced = adapter.manage_orders(
                "replace_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1", "new_price": 2}],
                market_version=7,
                async_request=True,
            )

            with self.assertRaises(MarketConfigurationError):
                adapter.manage_orders("cancel_orders", confirm_global_cancel="cancel all bets")
            global_cancel = adapter.manage_orders("cancel_orders", confirm_global_cancel="CANCEL ALL BETS")

        self.assertEqual(cancelled["response"]["status"], "SUCCESS")
        self.assertEqual(updated["response"]["instructionReports"][0]["betId"], "bet-1")
        self.assertEqual(replaced["request"]["marketVersion"], {"version": 7})
        self.assertTrue(replaced["request"]["async"])
        self.assertEqual(global_cancel["request"], {})
        self.assertEqual(calls[0]["method"], "SportsAPING/v1.0/cancelOrders")
        self.assertEqual(calls[0]["params"]["instructions"], [{"betId": "bet-1", "sizeReduction": 1.25}])
        self.assertEqual(calls[1]["method"], "SportsAPING/v1.0/updateOrders")
        self.assertEqual(calls[1]["params"]["instructions"], [{"betId": "bet-1", "newPersistenceType": "PERSIST"}])
        self.assertEqual(calls[2]["method"], "SportsAPING/v1.0/replaceOrders")
        self.assertEqual(calls[2]["params"]["instructions"], [{"betId": "bet-1", "newPrice": 2.0}])
        self.assertEqual(calls[3]["method"], "SportsAPING/v1.0/cancelOrders")
        self.assertEqual(calls[3]["params"], {})

        disabled = BetfairExchangeAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        with self.assertRaises(MarketConfigurationError):
            disabled.manage_orders("cancel_orders", market_id="1.234", instructions=[{"bet_id": "bet-1"}])
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "update_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1", "new_persistence_type": "UNKNOWN"}],
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "replace_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1", "new_price": 1.0}],
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.manage_orders(
                "cancel_orders",
                market_id="1.234",
                instructions=[{"bet_id": "bet-1"}],
                async_request=True,
            )


if __name__ == "__main__":
    unittest.main()
