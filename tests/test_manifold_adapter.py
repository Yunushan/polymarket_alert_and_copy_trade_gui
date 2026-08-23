from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import ManifoldAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manifold"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class ManifoldAdapterTests(unittest.TestCase):
    def make_adapter(self) -> ManifoldAdapter:
        adapter = ManifoldAdapter()
        search = load_fixture("search_markets")
        market_binary = load_fixture("market_binary")
        market_multi = load_fixture("market_multi")
        prob_binary = load_fixture("prob_binary")
        prob_multi = load_fixture("prob_multi")
        bets_activity = load_fixture("bets_activity")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/search-markets"):
                return search["results"]
            if url.endswith("/market/mf-binary-1"):
                return market_binary
            if url.endswith("/market/mf-multi-1"):
                return market_multi
            if url.endswith("/market/mf-binary-1/prob"):
                return prob_binary
            if url.endswith("/market/mf-multi-1/prob"):
                return prob_multi
            if url.endswith("/bets"):
                return bets_activity
            raise AssertionError(f"unexpected Manifold URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_metadata_advertises_documented_manifold_capabilities(self) -> None:
        adapter = ManifoldAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "manifold")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertIn("api.manifold.markets", health["api_base_url"])
        self.assertTrue(health["activity_feed_supported"])

    def test_list_events_uses_search_endpoint_and_maps_markets(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("launch", limit=5)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].market_id, "manifold")
        self.assertEqual(events[0].event_id, "mf-binary-1")
        self.assertEqual(events[0].status, "open")
        self.assertIn("demo launch", events[0].title)

    def test_list_contracts_maps_binary_and_multiple_choice_markets(self) -> None:
        adapter = self.make_adapter()

        binary_contracts = adapter.list_contracts("mf-binary-1")
        multi_contracts = adapter.list_contracts("mf-multi-1")

        self.assertEqual([contract.contract_id for contract in binary_contracts], ["mf-binary-1:YES", "mf-binary-1:NO"])
        self.assertEqual([contract.contract_id for contract in multi_contracts], ["mf-multi-1:ANSWER:answer-a", "mf-multi-1:ANSWER:answer-b"])
        self.assertEqual(multi_contracts[0].outcome, "Alpha")

    def test_get_price_supports_binary_yes_no_and_answer_probabilities(self) -> None:
        adapter = self.make_adapter()

        yes_price = adapter.get_price("mf-binary-1:YES")
        no_price = adapter.get_price("mf-binary-1:NO")
        answer_price = adapter.get_price("mf-multi-1:ANSWER:answer-b")

        self.assertEqual(yes_price.last, 0.62)
        self.assertAlmostEqual(no_price.last or 0, 0.38)
        self.assertEqual(answer_price.last, 0.65)
        self.assertEqual(answer_price.source, "manifold_probability")

    def test_orderbook_is_clear_unsupported_feature(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_orderbook("mf-binary-1:YES")

        self.assertEqual(ctx.exception.market_id, "manifold")
        self.assertEqual(ctx.exception.feature, "orderbook_reading")

    def test_public_bet_activity_is_normalized_for_copy_simulation(self) -> None:
        adapter = self.make_adapter()

        activity = adapter.list_activity("manifold:ForecastUser", limit=10)

        self.assertEqual(len(activity), 3)
        self.assertEqual(activity[0]["proxyWallet"], "manifold:forecastuser")
        self.assertEqual(activity[0]["side"], "BUY")
        self.assertAlmostEqual(activity[0]["size"], 12.5)
        self.assertEqual(activity[0]["asset"], "mf-binary-1:YES")
        self.assertEqual(activity[1]["side"], "SELL")
        self.assertAlmostEqual(activity[1]["size"], 6.25)
        self.assertEqual(activity[1]["shares"], 6.25)
        self.assertEqual(activity[2]["asset"], "mf-multi-1:ANSWER:answer-a")
        self.assertEqual(activity[2]["price"], 0.35)
        self.assertEqual(activity[0]["timestamp"], 1760000010)
        self.assertTrue(activity[0]["transactionHash"].startswith("manifold-bet:"))

    def test_public_trade_history_normalizes_fills_and_documented_time_filters(self) -> None:
        adapter = ManifoldAdapter()
        trades_fixture = load_fixture("bets_trades")
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, params))
            if url.endswith("/bets"):
                return trades_fixture
            raise AssertionError(f"unexpected Manifold URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        trades = adapter.list_trades(
            "mf-binary-1:YES",
            limit=2000,
            before=1760000030,
            after=1760000000,
        )

        self.assertEqual(len(trades), 2)
        self.assertEqual([trade.trade_id for trade in trades], ["trade-bet-1:0", "trade-bet-1:1"])
        self.assertEqual([trade.side for trade in trades], ["BUY", "BUY"])
        self.assertAlmostEqual(trades[0].price, 0.6, places=6)
        self.assertAlmostEqual(trades[1].size, 6.6666666667, places=6)
        self.assertEqual(trades[0].timestamp, 1760000015)
        self.assertEqual(trades[0].contract_id, "mf-binary-1:YES")
        self.assertEqual(
            calls[0][1],
            {
                "contractId": "mf-binary-1",
                "limit": 1000,
                "beforeTime": 1760000030000,
                "afterTime": 1760000000000,
            },
        )

        multi_trades = adapter.list_trades("mf-multi-1:ANSWER:answer-a")
        self.assertEqual(len(multi_trades), 1)
        self.assertEqual(multi_trades[0].contract_id, "mf-multi-1:ANSWER:answer-a")
        self.assertAlmostEqual(multi_trades[0].price, 0.6)

    def test_candle_history_derives_bounded_ohlcv_from_public_fills(self) -> None:
        adapter = ManifoldAdapter()
        trades_fixture = load_fixture("bets_trades")

        def fake_get_json(url: str, *, params=None, headers=None):
            if url.endswith("/bets"):
                return trades_fixture
            raise AssertionError(f"unexpected Manifold URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        candles = adapter.list_candles(
            "mf-binary-1:YES",
            resolution="1m",
            from_timestamp=1760000000,
            to_timestamp=1760000060,
        )

        self.assertEqual(len(candles), 1)
        candle = candles[0]
        self.assertEqual(candle.timestamp, 1759999980.0)
        self.assertAlmostEqual(candle.open, 0.6)
        self.assertAlmostEqual(candle.high, 0.6)
        self.assertAlmostEqual(candle.low, 0.6)
        self.assertAlmostEqual(candle.close, 0.6)
        self.assertAlmostEqual(candle.volume or 0, 10.0)
        self.assertTrue(candle.raw["derived"])
        self.assertEqual(candle.raw["source"], "manifold_public_bet_fills")
        self.assertEqual(candle.raw["trade_ids"], ["trade-bet-1:0", "trade-bet-1:1"])

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("mf-binary-1:YES", resolution="2h")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("mf-binary-1:YES", from_timestamp=10, to_timestamp=9)

    def test_activity_requires_prefixed_safe_manifold_identity(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("ForecastUser")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("manifold:../etc/passwd")

    def test_copy_trade_from_activity_builds_manifold_sell_paper_intent(self) -> None:
        adapter = self.make_adapter()
        activity = adapter.list_activity("manifold:forecastuser")[1]

        result = adapter.copy_trade_from_activity(activity)

        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["endpoint"], "/market/mf-binary-1/sell")
        self.assertEqual(result.raw["request"], {"shares": 6.25, "outcome": "NO"})

    def test_paper_order_builds_documented_dry_run_payload(self) -> None:
        adapter = self.make_adapter()
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="manifold",
                contract_id="mf-binary-1:YES",
                side="BUY",
                size=10,
                limit_price=0.62,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertEqual(result.raw["endpoint"], "/bet")
        self.assertTrue(result.raw["request"]["dryRun"])
        self.assertEqual(result.raw["request"]["limitProb"], 0.62)

    def test_order_validation_rejects_bad_inputs(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="manifold",
                    contract_id="mf-binary-1:YES",
                    side="BUY",
                    size=10,
                    limit_price=0.625,
                )
            )

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="manifold",
                    contract_id="mf-binary-1:MAYBE",
                    side="BUY",
                    size=10,
                    limit_price=0.62,
                )
            )

    def test_live_trading_is_disabled_by_default(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="manifold",
                    contract_id="mf-binary-1:YES",
                    side="BUY",
                    size=10,
                    limit_price=0.62,
                )
            )

        self.assertIn("disabled", str(ctx.exception))

    def test_live_buy_posts_with_api_key_when_enabled(self) -> None:
        adapter = ManifoldAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return {"id": "bet-1", "contractId": "mf-binary-1", "outcome": "YES"}

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            result = adapter.place_live_order(
                PaperOrderRequest(
                    market_id="manifold",
                    contract_id="mf-binary-1:YES",
                    side="BUY",
                    size=10,
                    limit_price=0.62,
                )
            )

        self.assertEqual(result["response"]["id"], "bet-1")
        method, url, payload, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/bet"))
        self.assertEqual(payload["contractId"], "mf-binary-1")
        self.assertEqual(payload["outcome"], "YES")
        self.assertFalse(payload["dryRun"])
        self.assertEqual(headers["Authorization"], "Key unit-test-key")

    def test_live_sell_posts_to_documented_sell_endpoint(self) -> None:
        adapter = ManifoldAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return {"sold": True}

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]

        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="manifold",
                    contract_id="mf-binary-1:NO",
                    side="SELL",
                    size=4,
                )
            )

        method, url, payload, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/market/mf-binary-1/sell"))
        self.assertEqual(payload, {"shares": 4.0, "outcome": "NO"})
        self.assertEqual(headers["Authorization"], "Key unit-test-key")

    def test_live_buy_for_single_answer_is_blocked_until_documented_shape_maps_safely(self) -> None:
        adapter = ManifoldAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})

        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            with self.assertRaises(MarketConfigurationError) as ctx:
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="manifold",
                        contract_id="mf-multi-1:ANSWER:answer-a",
                        side="BUY",
                        size=10,
                    )
                )

        self.assertIn("multi-bet", str(ctx.exception))

    def test_authenticated_account_reads_use_documented_me_and_bets_filters(self) -> None:
        adapter = ManifoldAdapter()
        me = load_fixture("me")
        bets = load_fixture("bets_account")
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, params, headers))
            if url.endswith("/me"):
                return me
            if url.endswith("/bets"):
                return bets
            raise AssertionError(f"unexpected Manifold URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            account = adapter.account_recovery("account")
            active = adapter.account_recovery(
                "active_orders",
                contract_id="mf-binary-1:YES",
                limit=2000,
                before="bet-open-1",
                after_time=1760000000,
            )
            history = adapter.account_recovery("order_history", limit=10, before_time=1760000030)

        self.assertEqual(account["id"], "user-123")
        self.assertEqual(active["response"], bets)
        self.assertEqual(active["parameters"]["userId"], "user-123")
        self.assertEqual(active["parameters"]["contractId"], "mf-binary-1")
        self.assertEqual(active["parameters"]["kinds"], "open-limit")
        self.assertEqual(active["parameters"]["limit"], 1000)
        self.assertEqual(active["parameters"]["afterTime"], 1760000000000)
        self.assertNotIn("kinds", history["parameters"])
        self.assertEqual(calls[0][2]["Authorization"], "Key unit-test-key")
        self.assertTrue(calls[2][0].endswith("/bets"))

    def test_account_reads_reject_unsafe_ids_and_reversed_time_bounds(self) -> None:
        adapter = ManifoldAdapter()
        adapter.runtime.get_json = lambda url, *, params=None, headers=None: load_fixture("me")  # type: ignore[method-assign]
        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery("active_orders", before="../outside")
            with self.assertRaises(MarketConfigurationError):
                adapter.account_recovery(
                    "order_history",
                    before_time=10,
                    after_time=20,
                )

    def test_guarded_open_limit_cancellation_uses_fixed_endpoint_and_confirmation(self) -> None:
        adapter = ManifoldAdapter(
            {
                "manifold_order_management_enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, params, json_body, headers))
            return load_fixture("cancel_response")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            result = adapter.manage_orders(
                "cancel_order",
                order_id="bet-open-1",
                confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            )

        self.assertTrue(result["live"])
        self.assertEqual(result["response"]["isCancelled"], True)
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/bet/bet-open-1/cancel"))
        self.assertEqual(calls[0][4]["Authorization"], "Key unit-test-key")
        with patch.dict("os.environ", {"MANIFOLD_API_KEY": "unit-test-key"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.manage_orders(
                    "cancel_order",
                    order_id="../unsafe",
                    confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                )
            with self.assertRaises(MarketConfigurationError):
                adapter.manage_orders("cancel_order", order_id="bet-open-1")


if __name__ == "__main__":
    unittest.main()

