from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import AzuroAdapter, PaperOrderRequest
from market_adapters.errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "azuro"
GAME_ID = "30061006000000000029214016"
CONDITION_ID = "300610060000000000649714110000000000000227249395"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class AzuroAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None) -> AzuroAdapter:
        adapter = AzuroAdapter(config)
        games_by_filters = load_fixture("games_by_filters")
        games_by_ids = load_fixture("games_by_ids")
        conditions = load_fixture("conditions_by_game_ids")
        order_response = load_fixture("order_response")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            if url.endswith("/market-manager/games-by-filters"):
                self.assertEqual(method, "POST")
                return games_by_filters
            if url.endswith("/market-manager/search-games"):
                self.assertEqual(method, "POST")
                return {"games": [games_by_filters["games"][0]], "page": 1, "perPage": 50, "total": 1, "totalPages": 1}
            if url.endswith("/market-manager/games-by-ids"):
                self.assertEqual(json_body["gameIds"], [GAME_ID])
                return games_by_ids
            if url.endswith("/market-manager/conditions-by-game-ids"):
                self.assertEqual(json_body["gameIds"], [GAME_ID])
                return conditions
            if url.endswith("/bet/orders/ordinar"):
                return order_response
            raise AssertionError(f"unexpected Azuro URL: {method} {url}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def test_registered_metadata_advertises_supported_azuro_features(self) -> None:
        adapter = AzuroAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "azuro")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(adapter.capabilities.copy_trading)
        self.assertIn("api.onchainfeed.org", health["api_base_url"])
        self.assertIn("streams.onchainfeed.org", health["websocket_url"])
        self.assertEqual(health["account_recovery_operations"], ["bet_history"])
        self.assertIn("thegraph.onchainfeed.org", health["graph_api_url"])

    def test_account_recovery_reads_documented_v3_and_live_bet_history(self) -> None:
        adapter = AzuroAdapter({"azuro_graph_api_url": "https://graph.fixture.test/query"})
        response = load_fixture("bet_history")
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return response

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        wallet = "0x0000000000000000000000000000000000000001"
        result = adapter.account_recovery("bet_history", wallet=wallet, limit=25, offset=4)

        self.assertEqual(result["source"], "azuro_client_subgraph")
        self.assertEqual(result["bettor"], wallet)
        self.assertEqual(result["limit"], 25)
        self.assertEqual(result["offset"], 4)
        self.assertEqual(result["v3_bets"][0]["betId"], "7")
        self.assertEqual(result["live_bets"][0]["betId"], "8")
        self.assertEqual(calls[0][0:2], ("POST", "https://graph.fixture.test/query"))
        self.assertEqual(calls[0][2]["variables"], {"bettor": wallet, "first": 25, "skip": 4})
        self.assertEqual(calls[0][3]["Content-Type"], "application/json")

    def test_account_recovery_rejects_invalid_wallet_bounds_and_graphql_errors(self) -> None:
        adapter = AzuroAdapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("bet_history", wallet="not-a-wallet")
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery(
                "bet_history",
                wallet="0x0000000000000000000000000000000000000001",
                limit=1001,
            )

        adapter.runtime.request_json = lambda *args, **kwargs: {"errors": [{"message": "query rejected"}]}  # type: ignore[method-assign]
        with self.assertRaises(MarketHTTPError):
            adapter.account_recovery(
                "bet_history",
                wallet="0x0000000000000000000000000000000000000001",
            )

    def test_list_events_uses_games_by_filters_and_search_games(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events(limit=10)
        searched = adapter.list_events("arsenal", limit=10)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].market_id, "azuro")
        self.assertEqual(events[0].event_id, GAME_ID)
        self.assertEqual(events[0].status, "prematch")
        self.assertEqual(len(searched), 1)
        self.assertIn("Arsenal", searched[0].title)

    def test_list_contracts_maps_conditions_and_outcomes(self) -> None:
        adapter = self.make_adapter()

        contracts = adapter.list_contracts(GAME_ID)

        self.assertEqual(len(contracts), 5)
        self.assertEqual(contracts[0].contract_id, f"{GAME_ID}:{CONDITION_ID}:29")
        self.assertEqual(contracts[0].outcome, "Arsenal")
        self.assertIn("Full Time Result", contracts[0].title)

    def test_get_price_converts_decimal_odds_to_probability(self) -> None:
        adapter = self.make_adapter()

        price = adapter.get_price(f"{GAME_ID}:{CONDITION_ID}:29")

        self.assertAlmostEqual(price.last or 0, 1 / 1.85)
        self.assertEqual(price.raw["decimal_odds"], 1.85)
        self.assertEqual(price.source, "azuro_current_odds")

    def test_orderbook_remains_unsupported_for_vamm_markets(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(UnsupportedFeatureError) as orderbook_ctx:
            adapter.get_orderbook(f"{GAME_ID}:{CONDITION_ID}:29")
        self.assertEqual(orderbook_ctx.exception.feature, "orderbook_reading")
        self.assertIn("vAMM", str(orderbook_ctx.exception))


    def test_bettor_activity_normalizes_single_bets_and_supports_paper_copy(self) -> None:
        adapter = AzuroAdapter({"azuro_graph_api_url": "https://graph.fixture.test/query"})
        response = load_fixture("bet_history")
        wallet = "0x0000000000000000000000000000000000000001"

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual((method, url), ("POST", "https://graph.fixture.test/query"))
            return response

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        activities = adapter.list_activity(wallet, limit=10)

        self.assertEqual(len(activities), 2)
        self.assertEqual([item["betId"] for item in activities], ["8", "7"])
        self.assertEqual(activities[0]["contract_id"], f"{GAME_ID}:{CONDITION_ID}:29")
        self.assertEqual(activities[0]["side"], "BUY")
        self.assertAlmostEqual(activities[0]["size"], 10.0)
        self.assertAlmostEqual(activities[0]["odds"], 1.85)
        self.assertAlmostEqual(activities[0]["price"], 1 / 1.85)
        self.assertEqual(activities[0]["source"], "azuro_bettor_bet_history")
        self.assertEqual(activities[0]["transactionHash"], "0xlivebet")

        result = adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(result.accepted)
        self.assertEqual(result.raw["paper_stake"], 10.0)
        self.assertEqual(result.raw["min_odds"], "1850000000000")

    def test_bettor_activity_skips_combo_rows_and_rejects_invalid_copy_inputs(self) -> None:
        adapter = AzuroAdapter({"azuro_graph_api_url": "https://graph.fixture.test/query"})
        response = load_fixture("bet_history")
        response["data"]["v3Bets"].append(
            {
                **response["data"]["v3Bets"][0],
                "betId": "combo",
                "selections": [
                    response["data"]["v3Bets"][0]["selections"][0],
                    response["data"]["v3Bets"][0]["selections"][0],
                ],
            }
        )
        adapter.runtime.request_json = lambda *args, **kwargs: response  # type: ignore[method-assign]
        wallet = "0x0000000000000000000000000000000000000001"
        activities = adapter.list_activity(wallet, limit=10)
        self.assertNotIn("combo", [item["betId"] for item in activities])

        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({"asset": "x", "side": "SELL", "size": 1, "odds": 1.5})
        with self.assertRaises(MarketConfigurationError):
            adapter.copy_trade_from_activity({"asset": "x", "side": "BUY", "size": 1, "odds": 0})

    def test_account_scoped_trade_history_and_derived_candles(self) -> None:
        wallet = "0x0000000000000000000000000000000000000001"
        adapter = AzuroAdapter(
            {
                "azuro_graph_api_url": "https://graph.fixture.test/query",
                "azuro_bettor_address": wallet,
            }
        )
        response = load_fixture("bet_history")
        adapter.runtime.request_json = lambda *args, **kwargs: response  # type: ignore[method-assign]

        trades = adapter.list_trades(f"{GAME_ID}:{CONDITION_ID}:29", limit=10)
        candles = adapter.list_candles(
            f"{GAME_ID}:{CONDITION_ID}:29",
            resolution="1h",
            from_timestamp=1773510000,
            to_timestamp=1773513600,
        )

        self.assertEqual([trade.trade_id for trade in trades], ["azuro:8", "azuro:7"])
        self.assertEqual([trade.side for trade in trades], ["BUY", "BUY"])
        self.assertAlmostEqual(trades[0].price, 1 / 1.85)
        self.assertAlmostEqual(trades[0].size, 10.0)
        self.assertTrue(trades[0].raw["account_scoped"])
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp, 1773511200.0)
        self.assertAlmostEqual(candles[0].open, 1 / 1.85)
        self.assertAlmostEqual(candles[0].close, 1 / 1.85)
        self.assertAlmostEqual(candles[0].volume or 0.0, 20.0)
        self.assertTrue(candles[0].raw["derived"])

    def test_account_scoped_history_validates_identity_and_bounds(self) -> None:
        adapter = AzuroAdapter({"azuro_bettor_address": "not-a-wallet"})
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(f"{GAME_ID}:{CONDITION_ID}:29")
        adapter = AzuroAdapter(
            {
                "azuro_bettor_address": "0x0000000000000000000000000000000000000001",
            }
        )
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(f"{GAME_ID}:{CONDITION_ID}:29", limit=1001)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(f"{GAME_ID}:{CONDITION_ID}:29", resolution="30m")
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(f"{GAME_ID}:{CONDITION_ID}:29", before=10, after=20)

    def test_paper_order_returns_official_calculation_request_shape(self) -> None:
        adapter = self.make_adapter()

        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="azuro",
                contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                side="BUY",
                size=10,
                limit_price=1.85,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertEqual(result.raw["calculation_request"]["environment"], "PolygonUSDT")
        self.assertEqual(result.raw["calculation_request"]["bets"][0]["conditionId"], CONDITION_ID)
        self.assertEqual(result.raw["calculation_request"]["bets"][0]["outcomeId"], 29)
        self.assertEqual(result.raw["min_odds"], "1850000000000")

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="azuro",
                    contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                    side="SELL",
                    size=10,
                    limit_price=1.85,
                )
            )

    def test_websocket_connection_info_uses_documented_subscription_messages(self) -> None:
        adapter = self.make_adapter()

        info = adapter.websocket_connection_info(game_ids=[GAME_ID], condition_ids=[CONDITION_ID])

        self.assertEqual(info["url"], "wss://streams.onchainfeed.org/v1/streams/feed")
        self.assertEqual(info["environment"], "PolygonUSDT")
        self.assertEqual(info["subscriptions"][0]["event"], "SubscribeGames")
        self.assertEqual(info["subscriptions"][1]["event"], "SubscribeConditions")
        self.assertEqual(info["subscriptions"][1]["data"]["conditionIds"], [CONDITION_ID])

        with self.assertRaises(MarketConfigurationError):
            adapter.websocket_connection_info()

    def test_live_order_is_fail_closed_before_credentials_or_network(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="azuro",
                    contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                    side="BUY",
                    size=10,
                    limit_price=1.85,
                )
            )

    def test_live_preflight_is_unsupported_even_when_configured(self) -> None:
        adapter = self.make_adapter({"live_trading_enabled": True, "live_trading_confirmed": True})

        with self.assertRaises(UnsupportedFeatureError):
            adapter.preflight_live_order(
                PaperOrderRequest(
                    market_id="azuro",
                    contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                    side="SELL",
                    size=10,
                    limit_price=1.85,
                )
            )

    def test_opaque_pre_signed_order_payload_is_not_forwarded(self) -> None:
        adapter = self.make_adapter({"live_trading_enabled": True, "live_trading_confirmed": True})
        client_bet_data = {
            "clientData": {
                "attention": "",
                "affiliate": "0x0000000000000000000000000000000000000000",
                "core": "0xF9548Be470A4e130c90ceA8b179FCD66D2972AC7",
                "expiresAt": 1773511660,
                "chainId": 137,
                "relayerFeeAmount": "10000",
                "isFeeSponsored": False,
                "isBetSponsored": False,
                "isSponsoredBetReturnable": False,
            },
            "bet": {
                "conditionId": CONDITION_ID,
                "outcomeId": 29,
                "minOdds": "1850000000000",
                "amount": "10000000",
                "nonce": "1",
            },
        }

        with patch.dict("os.environ", {"AZURO_BETTOR_ADDRESS": "0x0000000000000000000000000000000000000001"}):
            with self.assertRaises(UnsupportedFeatureError):
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="azuro",
                        contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                        side="BUY",
                        size=10,
                        limit_price=1.85,
                        metadata={"client_bet_data": client_bet_data, "bettor_signature": "0xsigned"},
                    )
                )

    def test_live_order_requires_wallet_signed_payload_metadata(self) -> None:
        adapter = self.make_adapter({"live_trading_enabled": True, "live_trading_confirmed": True})

        with patch.dict("os.environ", {"AZURO_BETTOR_ADDRESS": "0x0000000000000000000000000000000000000001"}):
            with self.assertRaises(UnsupportedFeatureError):
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="azuro",
                        contract_id=f"{GAME_ID}:{CONDITION_ID}:29",
                        side="BUY",
                        size=10,
                        limit_price=1.85,
                    )
                )



if __name__ == "__main__":
    unittest.main()
