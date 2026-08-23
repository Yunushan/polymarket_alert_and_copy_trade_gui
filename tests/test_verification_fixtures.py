from __future__ import annotations

import json
import base64
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "fixtures"


class VerificationFixtureTests(unittest.TestCase):
    def test_fixture_json_files_parse(self) -> None:
        fixture_paths = sorted(FIXTURE_ROOT.glob("**/*.json"))

        self.assertTrue(fixture_paths, "expected offline JSON fixtures")
        for path in fixture_paths:
            with self.subTest(path=path):
                data = json.loads(path.read_text(encoding="utf-8"))
                # Most fixtures wrap payloads in an object, while a few official
                # APIs (including IBKR Client Portal) return a top-level array.
                # Both are valid JSON response shapes; reject scalars/nulls.
                self.assertIsInstance(data, (dict, list))
                self.assertTrue(data, "fixture payload must not be empty")

    def test_polymarket_fixtures_cover_core_payload_shapes(self) -> None:
        market = json.loads((FIXTURE_ROOT / "polymarket" / "market.json").read_text(encoding="utf-8"))
        event = json.loads((FIXTURE_ROOT / "polymarket" / "event.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "polymarket" / "orderbook.json").read_text(encoding="utf-8"))
        activity = json.loads((FIXTURE_ROOT / "polymarket" / "activity_buy.json").read_text(encoding="utf-8"))
        clob_trades = json.loads((FIXTURE_ROOT / "polymarket" / "clob_trades.json").read_text(encoding="utf-8"))
        price_history = json.loads((FIXTURE_ROOT / "polymarket" / "price_history.json").read_text(encoding="utf-8"))

        self.assertIn("clobTokenIds", market)
        self.assertIn("outcomes", market)
        self.assertIsInstance(event.get("markets"), list)
        self.assertIn("bids", orderbook)
        self.assertIn("asks", orderbook)
        self.assertEqual(activity.get("side"), "BUY")
        self.assertIn("asset", activity)
        self.assertIsInstance(clob_trades.get("data"), list)
        self.assertIn("asset_id", clob_trades["data"][0])
        self.assertIsInstance(price_history.get("history"), list)
        self.assertIn("p", price_history["history"][0])

    def test_kalshi_fixtures_cover_core_payload_shapes(self) -> None:
        markets = json.loads((FIXTURE_ROOT / "kalshi" / "markets.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "kalshi" / "orderbook.json").read_text(encoding="utf-8"))
        trades = json.loads((FIXTURE_ROOT / "kalshi" / "trades.json").read_text(encoding="utf-8"))
        candles = json.loads((FIXTURE_ROOT / "kalshi" / "candlesticks.json").read_text(encoding="utf-8"))
        account_orders = json.loads((FIXTURE_ROOT / "kalshi" / "account_orders.json").read_text(encoding="utf-8"))
        account_fills = json.loads((FIXTURE_ROOT / "kalshi" / "account_fills.json").read_text(encoding="utf-8"))
        account_positions = json.loads((FIXTURE_ROOT / "kalshi" / "account_positions.json").read_text(encoding="utf-8"))
        account_settlements = json.loads((FIXTURE_ROOT / "kalshi" / "account_settlements.json").read_text(encoding="utf-8"))
        account_balance = json.loads((FIXTURE_ROOT / "kalshi" / "account_balance.json").read_text(encoding="utf-8"))
        account_queue = json.loads((FIXTURE_ROOT / "kalshi" / "account_queue_positions.json").read_text(encoding="utf-8"))
        cancel_order = json.loads((FIXTURE_ROOT / "kalshi" / "cancel_order_response.json").read_text(encoding="utf-8"))
        batch_cancel = json.loads((FIXTURE_ROOT / "kalshi" / "batch_cancel_orders_response.json").read_text(encoding="utf-8"))
        amend_order = json.loads((FIXTURE_ROOT / "kalshi" / "amend_order_response.json").read_text(encoding="utf-8"))
        decrease_order = json.loads((FIXTURE_ROOT / "kalshi" / "decrease_order_response.json").read_text(encoding="utf-8"))

        self.assertIsInstance(markets.get("markets"), list)
        self.assertGreaterEqual(len(markets["markets"]), 1)
        self.assertIn("ticker", markets["markets"][0])
        self.assertIn("event_ticker", markets["markets"][0])
        self.assertIn("orderbook_fp", orderbook)
        self.assertIn("yes_dollars", orderbook["orderbook_fp"])
        self.assertIn("no_dollars", orderbook["orderbook_fp"])
        self.assertIsInstance(trades.get("trades"), list)
        self.assertIn("trade_id", trades["trades"][0])
        self.assertIsInstance(candles.get("candlesticks"), list)
        self.assertIn("price", candles["candlesticks"][0])
        self.assertIsInstance(account_orders.get("orders"), list)
        self.assertIn("order_id", account_orders["orders"][0])
        self.assertIsInstance(account_fills.get("fills"), list)
        self.assertIn("fill_id", account_fills["fills"][0])
        self.assertIsInstance(account_positions.get("market_positions"), list)
        self.assertIn("ticker", account_positions["market_positions"][0])
        self.assertIsInstance(account_settlements.get("settlements"), list)
        self.assertIn("ticker", account_settlements["settlements"][0])
        self.assertIn("balance", account_balance)
        self.assertIsInstance(account_queue.get("queue_positions"), list)
        self.assertIn("market_ticker", account_queue["queue_positions"][0])
        self.assertEqual(cancel_order.get("order_id"), "order-kalshi-1")
        self.assertIsInstance(batch_cancel.get("orders"), list)
        self.assertEqual(amend_order.get("client_order_id"), "client-kalshi-1-updated")
        self.assertIn("remaining_count", decrease_order)

    def test_manifold_fixtures_cover_core_payload_shapes(self) -> None:
        search = json.loads((FIXTURE_ROOT / "manifold" / "search_markets.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "manifold" / "market_binary.json").read_text(encoding="utf-8"))
        multi = json.loads((FIXTURE_ROOT / "manifold" / "market_multi.json").read_text(encoding="utf-8"))
        prob = json.loads((FIXTURE_ROOT / "manifold" / "prob_binary.json").read_text(encoding="utf-8"))
        trades = json.loads((FIXTURE_ROOT / "manifold" / "bets_trades.json").read_text(encoding="utf-8"))

        self.assertIsInstance(search.get("results"), list)
        self.assertIn("id", search["results"][0])
        self.assertEqual(market.get("outcomeType"), "BINARY")
        self.assertIsInstance(multi.get("answers"), list)
        self.assertIn("prob", prob)
        self.assertIsInstance(trades, list)
        self.assertIn("fills", trades[0])

    def test_metaculus_fixtures_cover_core_payload_shapes(self) -> None:
        posts = json.loads((FIXTURE_ROOT / "metaculus" / "posts.json").read_text(encoding="utf-8"))
        binary = json.loads((FIXTURE_ROOT / "metaculus" / "post_binary.json").read_text(encoding="utf-8"))
        multiple = json.loads((FIXTURE_ROOT / "metaculus" / "post_multiple.json").read_text(encoding="utf-8"))
        numeric = json.loads((FIXTURE_ROOT / "metaculus" / "post_numeric.json").read_text(encoding="utf-8"))

        self.assertIsInstance(posts.get("results"), list)
        self.assertIn("question", binary)
        self.assertIn("aggregations", binary["question"])
        self.assertIsInstance(binary["question"]["aggregations"].get("recency_weighted", {}).get("history"), list)
        self.assertGreaterEqual(len(binary["question"]["aggregations"]["recency_weighted"]["history"]), 1)
        self.assertIsInstance(multiple.get("questions"), list)
        self.assertEqual(numeric["question"].get("type"), "numeric")

    def test_predictit_fixtures_cover_core_payload_shapes(self) -> None:
        all_markets = json.loads((FIXTURE_ROOT / "predictit" / "all.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "predictit" / "market.json").read_text(encoding="utf-8"))

        self.assertIsInstance(all_markets.get("markets"), list)
        self.assertGreaterEqual(len(all_markets["markets"]), 1)
        self.assertIn("contracts", all_markets["markets"][0])
        self.assertIsInstance(market.get("contracts"), list)
        self.assertIn("bestBuyYesCost", market["contracts"][0])
        self.assertIn("bestSellNoCost", market["contracts"][0])

    def test_limitless_fixtures_cover_core_payload_shapes(self) -> None:
        active = json.loads((FIXTURE_ROOT / "limitless_exchange" / "active.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "limitless_exchange" / "market.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "limitless_exchange" / "orderbook.json").read_text(encoding="utf-8"))
        historical_price = json.loads(
            (FIXTURE_ROOT / "limitless_exchange" / "historical_price.json").read_text(encoding="utf-8")
        )
        events = json.loads((FIXTURE_ROOT / "limitless_exchange" / "events.json").read_text(encoding="utf-8"))
        positions = json.loads((FIXTURE_ROOT / "limitless_exchange" / "positions.json").read_text(encoding="utf-8"))
        portfolio_history = json.loads(
            (FIXTURE_ROOT / "limitless_exchange" / "portfolio_history.json").read_text(encoding="utf-8")
        )
        user_orders = json.loads((FIXTURE_ROOT / "limitless_exchange" / "user_orders.json").read_text(encoding="utf-8"))

        self.assertIsInstance(active.get("data"), list)
        self.assertGreaterEqual(len(active["data"]), 1)
        self.assertIn("slug", active["data"][0])
        self.assertIn("positionIds", market)
        self.assertIn("tokens", market)
        self.assertIsInstance(orderbook.get("bids"), list)
        self.assertIsInstance(orderbook.get("asks"), list)
        self.assertIsInstance(historical_price.get("prices"), list)
        self.assertIn("timestamp", historical_price["prices"][0])
        self.assertIsInstance(events.get("events"), list)
        self.assertIn("tokenId", events["events"][0])
        self.assertIn("matchedSize", events["events"][0])
        self.assertIsInstance(positions.get("positions"), list)
        self.assertIn("marketSlug", positions["positions"][0])
        self.assertIsInstance(portfolio_history.get("history"), list)
        self.assertIn("timestamp", portfolio_history["history"][0])
        self.assertIsInstance(user_orders.get("orders"), list)
        self.assertIn("status", user_orders["orders"][0])

    def test_sx_bet_fixtures_cover_core_payload_shapes(self) -> None:
        active = json.loads((FIXTURE_ROOT / "sx_bet" / "active_markets.json").read_text(encoding="utf-8"))
        market_find = json.loads((FIXTURE_ROOT / "sx_bet" / "market_find.json").read_text(encoding="utf-8"))
        orders = json.loads((FIXTURE_ROOT / "sx_bet" / "orders.json").read_text(encoding="utf-8"))
        best_odds = json.loads((FIXTURE_ROOT / "sx_bet" / "best_odds.json").read_text(encoding="utf-8"))
        public_trades = json.loads((FIXTURE_ROOT / "sx_bet" / "public_trades.json").read_text(encoding="utf-8"))

        self.assertIsInstance(active.get("data", {}).get("markets"), list)
        self.assertIn("marketHash", active["data"]["markets"][0])
        self.assertIsInstance(market_find.get("data"), list)
        self.assertIsInstance(orders.get("data"), list)
        self.assertIn("percentageOdds", orders["data"][0])
        self.assertIsInstance(best_odds.get("data", {}).get("bestOdds"), list)
        self.assertIsInstance(public_trades.get("data", {}).get("trades"), list)
        self.assertIn("weightedAverageOdds", public_trades["data"]["trades"][0])

    def test_azuro_fixtures_cover_core_payload_shapes(self) -> None:
        games = json.loads((FIXTURE_ROOT / "azuro" / "games_by_filters.json").read_text(encoding="utf-8"))
        game = json.loads((FIXTURE_ROOT / "azuro" / "games_by_ids.json").read_text(encoding="utf-8"))
        conditions = json.loads((FIXTURE_ROOT / "azuro" / "conditions_by_game_ids.json").read_text(encoding="utf-8"))
        order = json.loads((FIXTURE_ROOT / "azuro" / "order_response.json").read_text(encoding="utf-8"))
        bet_history = json.loads((FIXTURE_ROOT / "azuro" / "bet_history.json").read_text(encoding="utf-8"))

        self.assertIsInstance(games.get("games"), list)
        self.assertIn("gameId", games["games"][0])
        self.assertIsInstance(game.get("games"), list)
        self.assertIsInstance(conditions.get("conditions"), list)
        self.assertIn("outcomes", conditions["conditions"][0])
        self.assertIn("currentOdds", conditions["conditions"][0]["outcomes"][0])
        self.assertIn("state", order)
        self.assertIsInstance(bet_history.get("data", {}).get("v3Bets"), list)
        self.assertIsInstance(bet_history.get("data", {}).get("liveBets"), list)
        self.assertIn("createdTxHash", bet_history["data"]["v3Bets"][0])
        self.assertIn("isRedeemable", bet_history["data"]["liveBets"][0])

    def test_legacy_web3_fixtures_cover_core_payload_shapes(self) -> None:
        augur_markets = json.loads((FIXTURE_ROOT / "augur" / "markets.json").read_text(encoding="utf-8"))
        omen_markets = json.loads((FIXTURE_ROOT / "omen" / "fpmms.json").read_text(encoding="utf-8"))
        gnosis_markets = json.loads(
            (FIXTURE_ROOT / "gnosis_prediction_markets" / "fpmms.json").read_text(encoding="utf-8")
        )
        zeitgeist_markets = json.loads((FIXTURE_ROOT / "zeitgeist" / "markets.json").read_text(encoding="utf-8"))
        zeitgeist_assets = json.loads((FIXTURE_ROOT / "zeitgeist" / "assets.json").read_text(encoding="utf-8"))
        pool_markets = json.loads(
            (FIXTURE_ROOT / "zeitgeist_prediction_pools" / "markets.json").read_text(encoding="utf-8")
        )
        pool_assets = json.loads(
            (FIXTURE_ROOT / "zeitgeist_prediction_pools" / "assets.json").read_text(encoding="utf-8")
        )
        reality_questions = json.loads(
            (FIXTURE_ROOT / "reality_eth_markets" / "questions.json").read_text(encoding="utf-8")
        )

        self.assertIsInstance(augur_markets.get("data", {}).get("markets"), list)
        self.assertIn("outcomes", augur_markets["data"]["markets"][0])
        self.assertIsInstance(omen_markets.get("data", {}).get("fixedProductMarketMakers"), list)
        self.assertIn("outcomeTokenMarginalPrices", omen_markets["data"]["fixedProductMarketMakers"][0])
        self.assertIsInstance(gnosis_markets.get("data", {}).get("fixedProductMarketMakers"), list)
        self.assertIn("outcomeTokenMarginalPrices", gnosis_markets["data"]["fixedProductMarketMakers"][0])
        self.assertIsInstance(zeitgeist_markets.get("data", {}).get("markets"), list)
        self.assertIn("outcomeAssets", zeitgeist_markets["data"]["markets"][0])
        self.assertIsInstance(zeitgeist_assets.get("data", {}).get("assets"), list)
        self.assertIn("price", zeitgeist_assets["data"]["assets"][0])
        self.assertIsInstance(pool_markets.get("data", {}).get("markets"), list)
        self.assertIn("pool", pool_markets["data"]["markets"][0])
        self.assertEqual(pool_markets["data"]["markets"][0]["pool"]["poolId"], 17)
        self.assertIsInstance(pool_assets.get("data", {}).get("assets"), list)
        self.assertEqual(pool_assets["data"]["assets"][0]["poolId"], 17)
        self.assertIsInstance(reality_questions.get("data", {}).get("questions"), list)
        self.assertIn("qJsonStr", reality_questions["data"]["questions"][0])
        self.assertIsInstance(reality_questions["data"]["questions"][0].get("outcomes"), list)

    def test_additional_official_adapter_fixtures_cover_core_payload_shapes(self) -> None:
        gemini_events = json.loads((FIXTURE_ROOT / "gemini" / "events.json").read_text(encoding="utf-8"))
        myriad_questions = json.loads((FIXTURE_ROOT / "myriad_markets" / "questions.json").read_text(encoding="utf-8"))
        myriad_market = json.loads((FIXTURE_ROOT / "myriad_markets" / "market.json").read_text(encoding="utf-8"))
        myriad_portfolio = json.loads((FIXTURE_ROOT / "myriad_markets" / "portfolio.json").read_text(encoding="utf-8"))
        myriad_market_positions = json.loads(
            (FIXTURE_ROOT / "myriad_markets" / "market_positions.json").read_text(encoding="utf-8")
        )
        opinion_markets = json.loads((FIXTURE_ROOT / "opinion_labs" / "markets.json").read_text(encoding="utf-8"))
        opinion_trades = json.loads((FIXTURE_ROOT / "opinion_labs" / "trades.json").read_text(encoding="utf-8"))
        opinion_orders = json.loads((FIXTURE_ROOT / "opinion_labs" / "orders.json").read_text(encoding="utf-8"))
        opinion_order_detail = json.loads(
            (FIXTURE_ROOT / "opinion_labs" / "order_detail.json").read_text(encoding="utf-8")
        )
        opinion_positions = json.loads((FIXTURE_ROOT / "opinion_labs" / "positions.json").read_text(encoding="utf-8"))
        opinion_price_history = json.loads(
            (FIXTURE_ROOT / "opinion_labs" / "price_history.json").read_text(encoding="utf-8")
        )
        predict_markets = json.loads((FIXTURE_ROOT / "predict_fun" / "markets.json").read_text(encoding="utf-8"))
        predict_matches = json.loads((FIXTURE_ROOT / "predict_fun" / "matches.json").read_text(encoding="utf-8"))
        xo_markets = json.loads((FIXTURE_ROOT / "xo_market" / "markets.json").read_text(encoding="utf-8"))
        betfair_catalogue = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "market_catalogue.json").read_text(encoding="utf-8")
        )
        myriad_orderbook = json.loads((FIXTURE_ROOT / "myriad_markets" / "orderbook.json").read_text(encoding="utf-8"))
        myriad_trades = json.loads((FIXTURE_ROOT / "myriad_markets" / "trades.json").read_text(encoding="utf-8"))
        myriad_cancel = json.loads((FIXTURE_ROOT / "myriad_markets" / "cancel_order_response.json").read_text(encoding="utf-8"))
        myriad_cancel_batch = json.loads((FIXTURE_ROOT / "myriad_markets" / "cancel_batch_response.json").read_text(encoding="utf-8"))
        myriad_cancel_all = json.loads((FIXTURE_ROOT / "myriad_markets" / "cancel_all_response.json").read_text(encoding="utf-8"))
        myriad_batch_modify = json.loads((FIXTURE_ROOT / "myriad_markets" / "batch_modify_response.json").read_text(encoding="utf-8"))
        gemini_order = json.loads((FIXTURE_ROOT / "gemini" / "order_response.json").read_text(encoding="utf-8"))
        gemini_cancel = json.loads((FIXTURE_ROOT / "gemini" / "cancel_order_response.json").read_text(encoding="utf-8"))
        gemini_batch_cancel = json.loads(
            (FIXTURE_ROOT / "gemini" / "batch_cancel_orders_response.json").read_text(encoding="utf-8")
        )
        predict_order = json.loads((FIXTURE_ROOT / "predict_fun" / "order_response.json").read_text(encoding="utf-8"))
        xmarket_batch_order = json.loads(
            (FIXTURE_ROOT / "xmarket" / "batch_order_response.json").read_text(encoding="utf-8")
        )
        xmarket_batch_cancel = json.loads(
            (FIXTURE_ROOT / "xmarket" / "batch_cancel_response.json").read_text(encoding="utf-8")
        )
        betfair_order = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "place_order_response.json").read_text(encoding="utf-8")
        )
        betfair_cancel = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "cancel_orders_response.json").read_text(encoding="utf-8")
        )
        betfair_update = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "update_orders_response.json").read_text(encoding="utf-8")
        )
        betfair_replace = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "replace_orders_response.json").read_text(encoding="utf-8")
        )
        betfair_cleared = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "cleared_orders.json").read_text(encoding="utf-8")
        )
        betfair_statement = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "account_statement.json").read_text(encoding="utf-8")
        )
        betfair_rates = json.loads(
            (FIXTURE_ROOT / "betfair_exchange" / "currency_rates.json").read_text(encoding="utf-8")
        )

        self.assertIsInstance(gemini_events.get("data"), list)
        self.assertIn("contracts", gemini_events["data"][0])
        self.assertIsInstance(myriad_questions.get("data"), list)
        self.assertIn("markets", myriad_questions["data"][0])
        self.assertIsInstance(myriad_market.get("outcomes"), list)
        self.assertIsInstance(myriad_market["outcomes"][0].get("price_charts"), dict)
        self.assertIsInstance(myriad_portfolio.get("data"), list)
        self.assertIn("shares", myriad_portfolio["data"][0])
        self.assertIsInstance(myriad_market_positions.get("data"), list)
        self.assertIn("positions", myriad_market_positions["data"][0])
        self.assertIsInstance(opinion_markets.get("result", {}).get("list"), list)
        self.assertIn("yesTokenId", opinion_markets["result"]["list"][0])
        self.assertIsInstance(opinion_trades.get("result", {}).get("list"), list)
        self.assertIn("tokenId", opinion_trades["result"]["list"][0])
        self.assertIsInstance(opinion_orders.get("result", {}).get("list"), list)
        self.assertIn("orderId", opinion_orders["result"]["list"][0])
        self.assertIn("orderData", opinion_order_detail.get("result", {}))
        self.assertIsInstance(opinion_positions.get("result", {}).get("list"), list)
        self.assertIn("tokenId", opinion_positions["result"]["list"][0])
        self.assertIsInstance(opinion_price_history.get("result", {}).get("history"), list)
        self.assertIn("p", opinion_price_history["result"]["history"][0])
        self.assertIsInstance(predict_markets.get("data"), list)
        self.assertIn("outcomes", predict_markets["data"][0])
        self.assertIsInstance(predict_matches.get("data"), list)
        self.assertIn("taker", predict_matches["data"][0])
        self.assertIn("priceExecuted", predict_matches["data"][0])
        self.assertIsInstance(xo_markets.get("markets"), list)
        self.assertIn("outcomes", xo_markets["markets"][0])
        self.assertIsInstance(betfair_catalogue.get("result"), list)
        self.assertIn("runners", betfair_catalogue["result"][0])
        self.assertIsInstance(myriad_orderbook.get("bids"), list)
        self.assertIsInstance(myriad_orderbook.get("asks"), list)
        self.assertIsInstance(myriad_trades, list)
        self.assertEqual(myriad_trades[0].get("side"), "buy")
        self.assertEqual(myriad_cancel.get("status"), "cancelled")
        self.assertIsInstance(myriad_cancel_batch.get("cancelled"), list)
        self.assertIsInstance(myriad_cancel_all.get("cancelled_count"), int)
        self.assertIsInstance(myriad_batch_modify.get("placed"), list)
        self.assertIn("orderId", gemini_order)
        self.assertEqual(gemini_cancel.get("result"), "ok")
        self.assertIsInstance(gemini_batch_cancel.get("results"), list)
        self.assertEqual(predict_order.get("success"), True)
        self.assertIsInstance(xmarket_batch_order.get("orders"), list)
        self.assertIn("id", xmarket_batch_order["orders"][0])
        self.assertIsInstance(xmarket_batch_cancel.get("cancelled"), list)
        self.assertEqual(xmarket_batch_cancel.get("status"), "cancelled")
        self.assertEqual(betfair_order.get("status"), "SUCCESS")
        for response in (betfair_cancel, betfair_update, betfair_replace):
            self.assertEqual(response.get("result", {}).get("status"), "SUCCESS")
            self.assertIsInstance(response.get("result", {}).get("instructionReports"), list)
        self.assertIsInstance(betfair_cleared.get("result", {}).get("clearedOrders"), list)
        self.assertIn("betId", betfair_cleared["result"]["clearedOrders"][0])
        self.assertIsInstance(betfair_statement.get("result", {}).get("accountStatement"), list)
        self.assertIn("refId", betfair_statement["result"]["accountStatement"][0])
        self.assertIsInstance(betfair_rates.get("result"), list)
        self.assertEqual(betfair_rates["result"][0].get("currencyCode"), "EUR")

    def test_iowa_electronic_markets_fixture_uses_documented_price_file_shape(self) -> None:
        market = json.loads((FIXTURE_ROOT / "iowa_electronic_markets" / "market.json").read_text(encoding="utf-8"))
        lines = (FIXTURE_ROOT / "iowa_electronic_markets" / "powell_price_data.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(market.get("archive_only"))
        self.assertEqual(len(market.get("contracts", [])), 2)
        self.assertGreaterEqual(len(lines), 1)
        fields = lines[0].split()
        self.assertGreaterEqual(len(fields), 9)
        self.assertRegex(fields[0], r"^\d{2}/\d{2}/\d{2}$")
        self.assertIn(fields[2], {"P.YES", "P.NO"})
        self.assertEqual(len(fields[3:9]), 6)

    def test_scicast_fixtures_cover_documented_datamart_shapes(self) -> None:
        questions = json.loads((FIXTURE_ROOT / "scicast" / "questions.json").read_text(encoding="utf-8"))
        history = json.loads((FIXTURE_ROOT / "scicast" / "question_history.json").read_text(encoding="utf-8"))
        trades = json.loads((FIXTURE_ROOT / "scicast" / "trade_history.json").read_text(encoding="utf-8"))

        self.assertIsInstance(questions.get("questions"), list)
        self.assertIn("question_id", questions["questions"][0])
        self.assertIsInstance(questions["questions"][0].get("choices"), list)
        self.assertIsInstance(history.get("history"), list)
        self.assertIn("probabilities", history["history"][0])
        self.assertIsInstance(trades.get("trades"), list)
        self.assertIn("trade_id", trades["trades"][0])
        self.assertIn("assets_per_option", trades["trades"][0])

    def test_probable_fixtures_cover_market_and_clob_payload_shapes(self) -> None:
        events = json.loads((FIXTURE_ROOT / "probable" / "events.json").read_text(encoding="utf-8"))
        event = json.loads((FIXTURE_ROOT / "probable" / "event.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "probable" / "market.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "probable" / "orderbook.json").read_text(encoding="utf-8"))
        activity = json.loads((FIXTURE_ROOT / "probable" / "activity.json").read_text(encoding="utf-8"))
        prices_history = json.loads((FIXTURE_ROOT / "probable" / "prices_history.json").read_text(encoding="utf-8"))
        open_orders = json.loads((FIXTURE_ROOT / "probable" / "open_orders.json").read_text(encoding="utf-8"))
        order_detail = json.loads((FIXTURE_ROOT / "probable" / "order_detail.json").read_text(encoding="utf-8"))
        cancel_order = json.loads((FIXTURE_ROOT / "probable" / "cancel_order_response.json").read_text(encoding="utf-8"))

        self.assertIsInstance(events.get("events"), list)
        self.assertIsInstance(event.get("markets"), list)
        self.assertIsInstance(market.get("tokens"), list)
        self.assertEqual(market["tokens"][0].get("outcome"), "Yes")
        self.assertEqual(market.get("condition_id"), "condition-1")
        self.assertIsInstance(orderbook.get("bids"), list)
        self.assertIsInstance(orderbook.get("asks"), list)
        self.assertIsInstance(activity, list)
        self.assertIn("transactionHash", activity[0])
        self.assertEqual(activity[0].get("type"), "TRADE")
        self.assertIsInstance(prices_history.get("history"), list)
        self.assertIn("t", prices_history["history"][0])
        self.assertIn("p", prices_history["history"][0])
        self.assertIsInstance(open_orders.get("orders"), list)
        self.assertIn("orderId", open_orders["orders"][0])
        self.assertIn("tokenId", order_detail)
        self.assertEqual(cancel_order.get("status"), "CANCELED")

    def test_matchbook_fixtures_cover_exchange_payload_shapes(self) -> None:
        events = json.loads((FIXTURE_ROOT / "matchbook" / "events.json").read_text(encoding="utf-8"))
        markets = json.loads((FIXTURE_ROOT / "matchbook" / "markets.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "matchbook" / "market.json").read_text(encoding="utf-8"))
        login = json.loads((FIXTURE_ROOT / "matchbook" / "login_response.json").read_text(encoding="utf-8"))
        order = json.loads((FIXTURE_ROOT / "matchbook" / "order_response.json").read_text(encoding="utf-8"))
        cancel_offer = json.loads((FIXTURE_ROOT / "matchbook" / "cancel_offer_response.json").read_text(encoding="utf-8"))
        cancel_offers = json.loads((FIXTURE_ROOT / "matchbook" / "cancel_offers_response.json").read_text(encoding="utf-8"))
        cancel_all = json.loads((FIXTURE_ROOT / "matchbook" / "cancel_all_offers_response.json").read_text(encoding="utf-8"))
        edit_offer = json.loads((FIXTURE_ROOT / "matchbook" / "edit_offer_response.json").read_text(encoding="utf-8"))
        edit_offers = json.loads((FIXTURE_ROOT / "matchbook" / "edit_offers_response.json").read_text(encoding="utf-8"))
        settled = json.loads((FIXTURE_ROOT / "matchbook" / "settled_bets.json").read_text(encoding="utf-8"))
        current = json.loads((FIXTURE_ROOT / "matchbook" / "current_bets.json").read_text(encoding="utf-8"))
        offers = json.loads((FIXTURE_ROOT / "matchbook" / "current_offers.json").read_text(encoding="utf-8"))
        balance = json.loads((FIXTURE_ROOT / "matchbook" / "balance.json").read_text(encoding="utf-8"))
        account = json.loads((FIXTURE_ROOT / "matchbook" / "account.json").read_text(encoding="utf-8"))

        self.assertIsInstance(events.get("events"), list)
        self.assertIsInstance(markets.get("markets"), list)
        self.assertIsInstance(market.get("runners"), list)
        self.assertIsInstance(market["runners"][0].get("prices"), list)
        self.assertIn("session-token", login)
        self.assertIsInstance(order.get("offers"), list)
        self.assertEqual(cancel_offer.get("offers")[0].get("status"), "cancelled")
        self.assertEqual(cancel_offers.get("offers")[1].get("id"), 405)
        self.assertEqual(cancel_all.get("cancelled"), "all")
        self.assertEqual(edit_offer.get("offers")[0].get("status"), "edited")
        self.assertEqual(edit_offers.get("offers")[0].get("new-stake"), 6.0)
        self.assertIsInstance(settled.get("data", {}).get("bets"), list)
        self.assertIsInstance(current.get("data", {}).get("bets"), list)
        self.assertIsInstance(offers.get("offers"), list)
        self.assertIn("balance", balance)
        self.assertIn("id", account)

    def test_betmgm_fixtures_cover_partner_sports_api_shapes(self) -> None:
        payload = json.loads((FIXTURE_ROOT / "betmgm" / "fixtures.json").read_text(encoding="utf-8"))

        self.assertIsInstance(payload.get("items"), list)
        fixture = payload["items"][0]
        self.assertIn("full", fixture["id"])
        self.assertIsInstance(fixture.get("markets"), list)
        market = fixture["markets"][0]
        self.assertIsInstance(market.get("options"), list)
        option = market["options"][0]
        self.assertIn("odds", option["price"])
        self.assertIn("fraction", option["price"])

    def test_dflow_fixtures_cover_nested_market_and_orderbook_shapes(self) -> None:
        events = json.loads((FIXTURE_ROOT / "dflow" / "events.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "dflow" / "orderbook.json").read_text(encoding="utf-8"))
        rpc = json.loads((FIXTURE_ROOT / "dflow" / "rpc_response.json").read_text(encoding="utf-8"))

        self.assertIsInstance(events.get("events"), list)
        market = events["events"][0]["markets"][0]
        self.assertIsInstance(market.get("accounts"), dict)
        self.assertIn("yesMint", next(iter(market["accounts"].values())))
        self.assertIsInstance(orderbook.get("yes_bids"), dict)
        self.assertIsInstance(orderbook.get("no_bids"), dict)
        self.assertEqual(rpc.get("result"), "signature-123")

    def test_context_v2_fixtures_cover_market_prices_orderbook_and_order_shapes(self) -> None:
        markets = json.loads((FIXTURE_ROOT / "context_v2" / "markets.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "context_v2" / "market.json").read_text(encoding="utf-8"))
        orderbook = json.loads((FIXTURE_ROOT / "context_v2" / "orderbook.json").read_text(encoding="utf-8"))
        activity = json.loads((FIXTURE_ROOT / "context_v2" / "activity.json").read_text(encoding="utf-8"))
        prices = json.loads((FIXTURE_ROOT / "context_v2" / "prices.json").read_text(encoding="utf-8"))
        order = json.loads((FIXTURE_ROOT / "context_v2" / "order_response.json").read_text(encoding="utf-8"))

        self.assertIsInstance(markets.get("markets"), list)
        self.assertEqual(market["outcomeTokens"][0].startswith("0x"), True)
        self.assertIsInstance(market.get("outcomePrices"), list)
        self.assertIsInstance(orderbook.get("outcomes"), list)
        self.assertEqual(activity["activity"][0]["type"], "trade")
        self.assertIsInstance(prices.get("prices"), list)
        self.assertEqual(order.get("success"), True)

    def test_smarkets_fixtures_cover_exchange_hierarchy_quotes_and_order_shapes(self) -> None:
        events = json.loads((FIXTURE_ROOT / "smarkets" / "events.json").read_text(encoding="utf-8"))
        markets = json.loads((FIXTURE_ROOT / "smarkets" / "markets.json").read_text(encoding="utf-8"))
        contracts = json.loads((FIXTURE_ROOT / "smarkets" / "contracts.json").read_text(encoding="utf-8"))
        quotes = json.loads((FIXTURE_ROOT / "smarkets" / "quotes.json").read_text(encoding="utf-8"))
        order = json.loads((FIXTURE_ROOT / "smarkets" / "order_response.json").read_text(encoding="utf-8"))
        account_orders = json.loads((FIXTURE_ROOT / "smarkets" / "orders.json").read_text(encoding="utf-8"))
        account = json.loads((FIXTURE_ROOT / "smarkets" / "account.json").read_text(encoding="utf-8"))
        cancellation = json.loads((FIXTURE_ROOT / "smarkets" / "cancel_response.json").read_text(encoding="utf-8"))

        self.assertIsInstance(events.get("events"), list)
        self.assertIsInstance(markets.get("markets"), list)
        self.assertIsInstance(contracts.get("contracts"), list)
        self.assertIsInstance(quotes["quotes"][0].get("back_offers"), list)
        self.assertIsInstance(order.get("orders"), list)
        self.assertIsInstance(account_orders.get("orders"), list)
        self.assertIsInstance(account.get("accounts"), list)
        self.assertEqual(cancellation.get("success"), True)

    def test_hedgehog_fixture_covers_market_v1_program_account_shape(self) -> None:
        payload = json.loads(
            (FIXTURE_ROOT / "hedgehog_markets" / "program_accounts.json").read_text(encoding="utf-8")
        )
        self.assertIsInstance(payload.get("result"), list)
        self.assertGreaterEqual(len(payload["result"]), 1)
        first = payload["result"][0]
        self.assertIn("pubkey", first)
        self.assertEqual(first["account"]["data"][1], "base64")
        self.assertEqual(first["account"]["owner"], "PARrVs6F5egaNuz8g6pKJyU4ze3eX5xGZCFb3GLiVvu")

    def test_frenzy_fixture_covers_bet_settled_log_shape(self) -> None:
        payload = json.loads((FIXTURE_ROOT / "frenzy_finance" / "rpc_responses.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["block_number"].get("result"), "0x200")
        logs = payload["settled_logs"].get("result")
        self.assertIsInstance(logs, list)
        self.assertGreaterEqual(len(logs), 1)
        self.assertEqual(len(logs[0].get("topics", [])), 4)
        self.assertEqual(logs[0]["topics"][0], "0x8d80dcfb835e27822f1d1a72e3f508bda35ef2bb31371a566c6dce20963761c7")
        self.assertEqual(len(logs[0].get("data", "")[2:]), 256)

    def test_thales_fixtures_cover_grouped_markets_quotes_and_amm_shapes(self) -> None:
        markets = json.loads((FIXTURE_ROOT / "thales_market" / "markets.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "thales_market" / "market.json").read_text(encoding="utf-8"))
        quote = json.loads((FIXTURE_ROOT / "thales_market" / "buy_quote.json").read_text(encoding="utf-8"))

        self.assertIsInstance(markets.get("BTC"), dict)
        grouped = markets["BTC"]["2026-12-31T12:00:00.000Z"]
        self.assertIsInstance(grouped.get("UP"), list)
        self.assertIn("address", grouped["UP"][0])
        self.assertEqual(market.get("position"), "UP")
        self.assertIn("price", market)
        self.assertIn("pricePerPosition", quote)

    def test_metadao_fixtures_cover_official_ticker_shapes(self) -> None:
        tickers = json.loads((FIXTURE_ROOT / "metadao" / "tickers.json").read_text(encoding="utf-8"))

        self.assertIsInstance(tickers.get("tickers"), list)
        self.assertIn("ticker_id", tickers["tickers"][0])
        self.assertIn("base_currency", tickers["tickers"][0])
        self.assertIn("last_price", tickers["tickers"][0])
        self.assertIn("liquidity_in_usd", tickers["tickers"][0])

    def test_seer_fixtures_cover_official_search_and_market_shapes(self) -> None:
        search = json.loads((FIXTURE_ROOT / "seer" / "markets_search.json").read_text(encoding="utf-8"))
        market = json.loads((FIXTURE_ROOT / "seer" / "market.json").read_text(encoding="utf-8"))

        self.assertIsInstance(search.get("markets"), list)
        self.assertEqual(search["markets"][0].get("chainId"), 100)
        self.assertTrue(search["markets"][0].get("id", "").startswith("0x"))
        self.assertIsInstance(search["markets"][0].get("odds"), list)
        self.assertEqual(market.get("outcomes"), ["Yes", "No"])
        self.assertEqual(len(market.get("wrappedTokens", [])), 2)

    def test_hyperliquid_fixtures_cover_hip4_metadata_book_activity_and_exchange_shapes(self) -> None:
        metadata = json.loads((FIXTURE_ROOT / "hyperliquid" / "outcome_meta.json").read_text(encoding="utf-8"))
        book = json.loads((FIXTURE_ROOT / "hyperliquid" / "l2_book.json").read_text(encoding="utf-8"))
        fills = json.loads((FIXTURE_ROOT / "hyperliquid" / "user_fills.json").read_text(encoding="utf-8"))
        candles = json.loads((FIXTURE_ROOT / "hyperliquid" / "candles.json").read_text(encoding="utf-8"))
        exchange = json.loads((FIXTURE_ROOT / "hyperliquid" / "exchange_response.json").read_text(encoding="utf-8"))
        cancel = json.loads((FIXTURE_ROOT / "hyperliquid" / "cancel_response.json").read_text(encoding="utf-8"))
        modify = json.loads((FIXTURE_ROOT / "hyperliquid" / "modify_response.json").read_text(encoding="utf-8"))
        schedule = json.loads((FIXTURE_ROOT / "hyperliquid" / "schedule_cancel_response.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["outcomes"][0].get("outcome"), 1)
        self.assertEqual([side["name"] for side in metadata["outcomes"][0]["sideSpecs"]], ["Yes", "No"])
        self.assertEqual(book.get("coin"), "#10")
        self.assertEqual(len(book.get("levels", [])), 2)
        self.assertEqual(fills[0].get("coin"), "#10")
        self.assertEqual(fills[1].get("coin"), "BTC")
        self.assertIsInstance(candles, list)
        self.assertEqual(candles[0].get("s"), "#10")
        self.assertIn("o", candles[0])
        self.assertIn("v", candles[0])
        self.assertEqual(exchange.get("status"), "ok")
        self.assertEqual(cancel["response"]["type"], "cancel")
        self.assertEqual(modify["response"]["type"], "batchModify")
        self.assertEqual(schedule["response"]["type"], "scheduleCancel")

    def test_hyperliquid_account_fixtures_cover_documented_info_shapes(self) -> None:
        open_orders = json.loads((FIXTURE_ROOT / "hyperliquid" / "open_orders.json").read_text(encoding="utf-8"))
        historical_orders = json.loads((FIXTURE_ROOT / "hyperliquid" / "historical_orders.json").read_text(encoding="utf-8"))
        clearinghouse = json.loads((FIXTURE_ROOT / "hyperliquid" / "clearinghouse_state.json").read_text(encoding="utf-8"))
        spot = json.loads((FIXTURE_ROOT / "hyperliquid" / "spot_clearinghouse_state.json").read_text(encoding="utf-8"))
        portfolio = json.loads((FIXTURE_ROOT / "hyperliquid" / "portfolio.json").read_text(encoding="utf-8"))
        subaccounts = json.loads((FIXTURE_ROOT / "hyperliquid" / "subaccounts.json").read_text(encoding="utf-8"))

        self.assertEqual(open_orders[0].get("coin"), "#10")
        self.assertEqual(historical_orders[0].get("status"), "filled")
        self.assertIn("marginSummary", clearinghouse)
        self.assertIsInstance(spot.get("balances"), list)
        self.assertEqual(portfolio[0][0], "day")
        self.assertEqual(subaccounts[0].get("master"), "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")

    def test_trueo_fixtures_cover_onchain_manager_pool_and_signed_transaction_shapes(self) -> None:
        rpc = json.loads((FIXTURE_ROOT / "trueo" / "rpc.json").read_text(encoding="utf-8"))

        self.assertTrue(rpc["manager"].startswith("0x"))
        self.assertTrue(rpc["market"].startswith("0x"))
        self.assertTrue(rpc["yesPool"].startswith("0x"))
        self.assertGreater(rpc["poolSqrtPriceX96"], "0")
        self.assertTrue(rpc["signedTransaction"].startswith("0x"))

    def test_prdt_fixture_covers_prediction_contract_rpc_shapes(self) -> None:
        rpc = json.loads((FIXTURE_ROOT / "prdt_finance" / "rpc_responses.json").read_text(encoding="utf-8"))

        self.assertTrue(rpc["prediction_address"].startswith("0x"))
        self.assertTrue(rpc["factory_address"].startswith("0x"))
        self.assertEqual(rpc["epoch"], 42)
        self.assertEqual(set(rpc["responses"]), {
            "current_epoch",
            "interval_seconds",
            "min_bet_amount",
            "bet_token",
            "oracle",
            "round",
            "timestamps",
        })
        for value in rpc["responses"].values():
            if isinstance(value, list):
                self.assertTrue(all(len(word) == 64 for word in value))
            else:
                self.assertTrue(value.startswith("0x"))

    def test_zetarium_fixture_covers_prediction_market_rpc_shapes(self) -> None:
        rpc = json.loads((FIXTURE_ROOT / "zetarium_world" / "rpc_responses.json").read_text(encoding="utf-8"))

        self.assertTrue(rpc["prediction_market_address"].startswith("0x"))
        self.assertTrue(rpc["stake_token"].startswith("0x"))
        self.assertEqual(set(rpc["markets"]), {"1", "2"})
        self.assertEqual(set(rpc["stakes"]), {"1:0", "1:1", "2:0", "2:1"})
        self.assertTrue(rpc["signed_transaction"].startswith("0x"))
        self.assertTrue(rpc["transaction_data"].startswith("0xda866c48"))

    def test_lamas_fixture_covers_anchor_round_result_shapes(self) -> None:
        rpc = json.loads((FIXTURE_ROOT / "lamas_finance" / "rpc_responses.json").read_text(encoding="utf-8"))

        self.assertEqual(rpc["account_discriminator"], "d80b15c4d5f075eb")
        for _game, rows in rpc["program_accounts"].items():
            self.assertEqual(len(rows), 1)
            row = rows[0]
            account = row["account"]
            self.assertEqual(account["owner"], row["program_id"] if "program_id" in row else account["owner"])
            self.assertEqual(account["data"][1], "base64")
            raw = base64.b64decode(account["data"][0], validate=True)
            self.assertGreater(len(raw), 8)
            self.assertEqual(raw[:8].hex(), rpc["account_discriminator"])

        self.assertEqual(set(rpc["account_info"]), {"up_or_down", "price_predict"})

    def test_ibkr_event_contract_fixtures_cover_forecastx_and_cme_shapes(self) -> None:
        forecast_search = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "search.json").read_text(encoding="utf-8"))
        forecast_strikes = json.loads((FIXTURE_ROOT / "forecastex" / "strikes.json").read_text(encoding="utf-8"))
        forecast_info = json.loads((FIXTURE_ROOT / "forecastex" / "info.json").read_text(encoding="utf-8"))
        cme_search = json.loads((FIXTURE_ROOT / "cme_prediction_markets" / "search.json").read_text(encoding="utf-8"))
        cme_info = json.loads((FIXTURE_ROOT / "cme_prediction_markets" / "info.json").read_text(encoding="utf-8"))
        snapshot = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "snapshot.json").read_text(encoding="utf-8"))
        history = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "history.json").read_text(encoding="utf-8"))
        trades = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "trades.json").read_text(encoding="utf-8"))
        orders = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "orders.json").read_text(encoding="utf-8"))
        order_status = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "order_status.json").read_text(encoding="utf-8"))
        cancel_response = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "cancel_response.json").read_text(encoding="utf-8"))
        modify_response = json.loads((FIXTURE_ROOT / "ibkr_forecasttrader" / "modify_response.json").read_text(encoding="utf-8"))

        self.assertEqual(forecast_search[0]["symbol"], "FF")
        self.assertIn(4.875, forecast_strikes["call"])
        self.assertEqual({record["right"] for record in forecast_info}, {"C", "P"})
        self.assertEqual(cme_search[0]["sections"][-1]["secType"], "EC")
        self.assertEqual({record["tradingClass"] for record in cme_info}, {"ECNQ", "Q4A"})
        self.assertEqual(snapshot[0]["conid"], 721095497)
        self.assertEqual(history["barLength"], 3600)
        self.assertEqual(len(history["data"]), 4)
        self.assertEqual(history["data"][1]["c"], 0.47)
        self.assertIsInstance(trades, list)
        self.assertEqual(trades[0]["execution_id"], "exec-event-buy-1")
        self.assertEqual(trades[0]["conid"], 721095497)
        self.assertEqual(orders["orders"][0]["orderId"], "987654")
        self.assertEqual(order_status["order_status"], "Submitted")
        self.assertEqual(cancel_response["msg"], "Request was submitted")
        self.assertEqual(modify_response[0]["order_status"], "Submitted")


if __name__ == "__main__":
    unittest.main()

