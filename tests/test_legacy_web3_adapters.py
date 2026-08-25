from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import (
    AugurAdapter,
    GnosisPredictionMarketsAdapter,
    OmenAdapter,
    PaperOrderRequest,
    RealityEthMarketsAdapter,
    ZeitgeistAdapter,
    ZeitgeistPredictionPoolsAdapter,
    ZeitgeistSdkMarketsAdapter,
)
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
AUGUR_MARKET_ID = "0xaugurmarket1"
OMEN_FPMM_ID = "0xomenfpmm1"
ZEITGEIST_MARKET_ID = "90"
REALITY_QUESTION_ENTITY_ID = "0xreality-question-1"


def load_fixture(market: str, name: str):
    return json.loads((FIXTURES / market / f"{name}.json").read_text(encoding="utf-8"))


class LegacyWeb3AdapterTests(unittest.TestCase):
    def make_augur(self) -> AugurAdapter:
        adapter = AugurAdapter({"augur_subgraph_url": "https://example.test/augur"})
        markets = load_fixture("augur", "markets")
        market = load_fixture("augur", "market")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://example.test/augur")
            query = json_body["query"]
            if "markets(first" in query:
                return markets
            if "market(id" in query:
                self.assertEqual(json_body["variables"]["id"], AUGUR_MARKET_ID)
                return market
            raise AssertionError(f"unexpected Augur query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def make_omen(self, extra_config=None) -> OmenAdapter:
        config = {"omen_subgraph_url": "https://example.test/omen"}
        config.update(extra_config or {})
        adapter = OmenAdapter(config)
        markets = load_fixture("omen", "fpmms")
        market = load_fixture("omen", "fpmm")
        trades = load_fixture("omen", "trades")
        token = load_fixture("omen", "token")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            if url == "https://rpc.example.test/omen":
                self.assertEqual(json_body["method"], "eth_sendRawTransaction")
                return {"jsonrpc": "2.0", "id": 1, "result": "0x" + "ab" * 32}
            self.assertEqual(url, "https://example.test/omen")
            query = json_body["query"]
            if "fpmmTrades" in query:
                if "OmenActivity" in query:
                    self.assertEqual(json_body["variables"]["creator"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                    self.assertEqual(json_body["variables"]["after"], "0")
                    self.assertEqual(json_body["variables"]["before"], "253402300799")
                else:
                    self.assertIn("creator", query)
                    self.assertEqual(json_body["variables"]["fpmm"], OMEN_FPMM_ID)
                    self.assertEqual(json_body["variables"]["outcomeIndex"], "0")
                return trades
            if "token(id" in query:
                self.assertEqual(
                    json_body["variables"]["id"],
                    "0x0000000000000000000000000000000000000001",
                )
                return token
            if "fixedProductMarketMakers" in query:
                return markets
            if "fixedProductMarketMaker" in query:
                self.assertEqual(json_body["variables"]["id"], OMEN_FPMM_ID)
                return market
            raise AssertionError(f"unexpected Omen query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def make_reality(self) -> RealityEthMarketsAdapter:
        adapter = RealityEthMarketsAdapter({"reality_eth_subgraph_url": "https://example.test/reality"})
        questions = load_fixture("reality_eth_markets", "questions")
        question = load_fixture("reality_eth_markets", "question")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://example.test/reality")
            query = json_body["query"]
            if "questions(first" in query:
                return questions
            if "question(id" in query:
                self.assertEqual(json_body["variables"]["id"], REALITY_QUESTION_ENTITY_ID)
                return question
            raise AssertionError(f"unexpected Reality.eth query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def make_gnosis(self) -> GnosisPredictionMarketsAdapter:
        adapter = GnosisPredictionMarketsAdapter({"gnosis_subgraph_url": "https://example.test/gnosis"})
        markets = load_fixture("gnosis_prediction_markets", "fpmms")
        market = load_fixture("gnosis_prediction_markets", "fpmm")
        trades = load_fixture("gnosis_prediction_markets", "trades")
        token = load_fixture("gnosis_prediction_markets", "token")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://example.test/gnosis")
            query = json_body["query"]
            if "fpmmTrades" in query:
                if "OmenActivity" in query:
                    self.assertEqual(json_body["variables"]["creator"], "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
                    self.assertEqual(json_body["variables"]["after"], "0")
                    self.assertEqual(json_body["variables"]["before"], "253402300799")
                else:
                    self.assertIn("creator", query)
                    self.assertEqual(json_body["variables"]["fpmm"], OMEN_FPMM_ID)
                    self.assertEqual(json_body["variables"]["outcomeIndex"], "0")
                return trades
            if "token(id" in query:
                self.assertEqual(
                    json_body["variables"]["id"],
                    "0x0000000000000000000000000000000000000001",
                )
                return token
            if "fixedProductMarketMakers" in query:
                return markets
            if "fixedProductMarketMaker" in query:
                self.assertEqual(json_body["variables"]["id"], OMEN_FPMM_ID)
                return market
            raise AssertionError(f"unexpected Gnosis query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def make_zeitgeist(self, extra_config=None) -> ZeitgeistAdapter:
        adapter = ZeitgeistAdapter(dict(extra_config or {}))
        markets = load_fixture("zeitgeist", "markets")
        market = load_fixture("zeitgeist", "market")
        assets = load_fixture("zeitgeist", "assets")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            if url == "https://rpc.example.test/zeitgeist":
                if json_body["method"] == "state_getRuntimeVersion":
                    return {"jsonrpc": "2.0", "id": 1, "result": {"specVersion": 57}}
                if json_body["method"] == "author_submitExtrinsic":
                    self.assertEqual(json_body["params"][0], "0x" + "ab" * 128)
                    return {"jsonrpc": "2.0", "id": 1, "result": "0x" + "12" * 32}
                raise AssertionError(f"unexpected Zeitgeist RPC method: {json_body['method']}")
            self.assertIn("processor.bsr.zeitgeist.pm/graphql", url)
            query = json_body["query"]
            if "ZeitgeistMarkets" in query:
                return markets
            if "ZeitgeistMarket" in query:
                self.assertEqual(json_body["variables"]["marketId"], int(ZEITGEIST_MARKET_ID))
                return market
            if "ZeitgeistAsset" in query:
                self.assertEqual(json_body["variables"]["assetId"], "CategoricalOutcome:90:0")
                return assets
            raise AssertionError(f"unexpected Zeitgeist query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def make_zeitgeist_pools(self) -> ZeitgeistPredictionPoolsAdapter:
        adapter = ZeitgeistPredictionPoolsAdapter(
            {"zeitgeist_pools_indexer_url": "https://example.test/zeitgeist-pools"}
        )
        markets = load_fixture("zeitgeist_prediction_pools", "markets")
        market = load_fixture("zeitgeist_prediction_pools", "market")
        assets = load_fixture("zeitgeist_prediction_pools", "assets")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://example.test/zeitgeist-pools")
            query = json_body["query"]
            if "ZeitgeistMarkets" in query:
                return markets
            if "ZeitgeistMarket" in query:
                self.assertEqual(json_body["variables"]["marketId"], int(ZEITGEIST_MARKET_ID))
                return market
            if "ZeitgeistAsset" in query:
                self.assertEqual(json_body["variables"]["assetId"], "CategoricalOutcome:90:0")
                return assets
            raise AssertionError(f"unexpected Zeitgeist pool query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        return adapter

    def test_augur_lists_markets_and_outcomes_from_configured_subgraph(self) -> None:
        adapter = self.make_augur()
        health = adapter.health_check()

        self.assertTrue(adapter.capabilities.event_listing)
        self.assertFalse(adapter.capabilities.price_reading)
        self.assertTrue(health["graphql_url_configured"])

        events = adapter.list_events("eth", limit=10)
        contracts = adapter.list_contracts(AUGUR_MARKET_ID)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, AUGUR_MARKET_ID)
        self.assertEqual(events[0].status, "trading")
        self.assertEqual(len(contracts), 3)
        self.assertEqual(contracts[2].outcome, "Yes")

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_price(f"{AUGUR_MARKET_ID}:0xaugurmarket1-2")
        self.assertEqual(ctx.exception.feature, "price_reading")

    def test_augur_requires_subgraph_endpoint_before_network_calls(self) -> None:
        adapter = AugurAdapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.list_events()

        self.assertIn("GraphQL endpoint", str(ctx.exception))

    def test_reality_eth_lists_questions_and_response_options_from_official_subgraph(self) -> None:
        adapter = self.make_reality()
        health = adapter.health_check()
        events = adapter.list_events("eth", limit=10)
        contracts = adapter.list_contracts(REALITY_QUESTION_ENTITY_ID)

        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertFalse(adapter.capabilities.alerts)
        self.assertFalse(adapter.capabilities.price_reading)
        self.assertTrue(health["question_schema_supported"])
        self.assertEqual(health["graphql_url_source"], "config:reality_eth_subgraph_url")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, REALITY_QUESTION_ENTITY_ID)
        self.assertEqual(events[0].status, "open")
        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].outcome, "Yes")

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_price(f"{REALITY_QUESTION_ENTITY_ID}:0xreality-question-1:yes")
        self.assertEqual(ctx.exception.feature, "price_reading")

    def test_reality_eth_requires_a_configured_subgraph_endpoint(self) -> None:
        adapter = RealityEthMarketsAdapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.list_events()

        self.assertIn("GraphQL endpoint", str(ctx.exception))

    def test_omen_reads_amm_marginal_prices_and_paper_orders(self) -> None:
        adapter = self.make_omen()

        events = adapter.list_events("gnosis", limit=10)
        contracts = adapter.list_contracts(OMEN_FPMM_ID)
        price = adapter.get_price(f"{OMEN_FPMM_ID}:0")
        paper = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="omen",
                contract_id=f"{OMEN_FPMM_ID}:0",
                side="BUY",
                size=12.5,
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "active")
        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertAlmostEqual(price.last or 0, 0.62)
        self.assertTrue(paper.accepted)
        self.assertAlmostEqual(paper.average_price or 0, 0.62)
        self.assertIn("DRY RUN", paper.message)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{OMEN_FPMM_ID}:0")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(market_id="omen", contract_id=f"{OMEN_FPMM_ID}:0", side="BUY", size=1)
            )

    def test_omen_normalizes_public_fpmm_trades_and_derived_candles(self) -> None:
        adapter = self.make_omen()

        trades = adapter.list_trades(
            f"{OMEN_FPMM_ID}:0",
            limit=2,
            after=1733316000,
            before=1733316400,
        )
        candles = adapter.list_candles(
            f"{OMEN_FPMM_ID}:0",
            resolution="1h",
            from_timestamp=1733316000,
            to_timestamp=1733316400,
        )

        self.assertTrue(adapter.capabilities.trade_history)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertEqual([trade.trade_id for trade in trades], ["0xomentrade2", "0xomentrade1"])
        self.assertEqual([trade.side for trade in trades], ["SELL", "BUY"])
        self.assertAlmostEqual(trades[0].price, 3.8 / 6.0)
        self.assertAlmostEqual(trades[0].size, 6.0)
        self.assertEqual(trades[0].raw["source"], "omen_fpmm_trades")
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp, 1733313600.0)
        self.assertAlmostEqual(candles[0].open, 0.6)
        self.assertAlmostEqual(candles[0].close, 3.8 / 6.0)
        self.assertAlmostEqual(candles[0].volume or 0.0, 10.0)
        self.assertTrue(candles[0].raw["derived"])
        self.assertEqual(candles[0].raw["trade_ids"], ["0xomentrade1", "0xomentrade2"])

    def test_omen_creator_activity_is_bounded_and_copy_is_simulation_first(self) -> None:
        adapter = self.make_omen()

        activities = adapter.list_activity(
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            limit=10,
        )

        self.assertEqual([row["trade_id"] for row in activities], ["0xomentrade2", "0xomentrade1"])
        self.assertTrue(all(row["proxyWallet"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" for row in activities))
        self.assertTrue(all(row["creator"] == row["proxyWallet"] for row in activities))
        self.assertTrue(all(row["source"] == "omen_fpmm_creator_trades" for row in activities))
        recovered = adapter.account_recovery(
            "activity",
            wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            limit=2,
        )
        self.assertEqual(recovered["source"], "omen_fpmm_creator_trades")
        self.assertEqual(recovered["endpoint"], "fpmmTrades(creator=...)")
        self.assertEqual(recovered["wallet"], "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(recovered["limit"], 2)
        self.assertEqual(recovered["activity"], activities)

        preview = adapter.copy_trade_from_activity(activities[0])
        self.assertTrue(preview.accepted)
        self.assertEqual(preview.contract_id, f"{OMEN_FPMM_ID}:0")
        self.assertAlmostEqual(preview.average_price or 0.0, 3.8 / 6.0)

        mismatched = dict(activities[0])
        mismatched["proxyWallet"] = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with self.assertRaisesRegex(MarketConfigurationError, "does not match"):
            adapter.copy_trade_from_activity(mismatched)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_activity("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", limit=0)
        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("positions", wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_omen_history_validation_fails_closed(self) -> None:
        adapter = self.make_omen()

        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(f"{OMEN_FPMM_ID}:0", limit=0)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_trades(f"{OMEN_FPMM_ID}:0", after=20, before=10)
        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles(f"{OMEN_FPMM_ID}:0", resolution="2h")

    def test_omen_uses_indexed_scale_for_each_collateral_token(self) -> None:
        adapter = OmenAdapter({"omen_subgraph_url": "https://example.test/omen"})
        fpmm_six = "0x" + "a" * 40
        fpmm_eighteen = "0x" + "b" * 40
        token_six = "0x" + "0" * 38 + "06"
        token_eighteen = "0x" + "0" * 38 + "18"
        token_queries = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            query = json_body["query"]
            variables = json_body["variables"]
            if "fpmmTrades" in query:
                fpmm = variables["fpmm"]
                collateral_token = token_six if fpmm == fpmm_six else token_eighteen
                scale = 10**6 if fpmm == fpmm_six else 10**18
                return {
                    "data": {
                        "fpmmTrades": [
                            {
                                "id": f"{fpmm}-trade",
                                "fpmm": {"id": fpmm},
                                "collateralToken": collateral_token,
                                "type": "Buy",
                                "creationTimestamp": "1733316060",
                                "collateralAmount": str(3 * scale),
                                "outcomeIndex": "0",
                                "outcomeTokensTraded": str(6 * scale),
                                "transactionHash": "0x" + "1" * 64,
                            }
                        ]
                    }
                }
            if "token(id" in query:
                token_id = variables["id"]
                token_queries.append(token_id)
                return {
                    "data": {
                        "token": {
                            "id": token_id,
                            "scale": str(10**6 if token_id == token_six else 10**18),
                        }
                    }
                }
            raise AssertionError(f"unexpected Omen query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        six_decimal_trade = adapter.list_trades(f"{fpmm_six}:0", limit=1)[0]
        eighteen_decimal_trade = adapter.list_trades(f"{fpmm_eighteen}:0", limit=1)[0]
        adapter.list_trades(f"{fpmm_six}:0", limit=1)

        self.assertEqual(six_decimal_trade.size, 6.0)
        self.assertEqual(eighteen_decimal_trade.size, 6.0)
        self.assertEqual(token_queries, [token_six, token_eighteen])

    def test_omen_candles_reject_ambiguous_same_second_prices(self) -> None:
        adapter = self.make_omen()
        trades = load_fixture("omen", "trades")
        token = load_fixture("omen", "token")
        trades["data"]["fpmmTrades"][1]["creationTimestamp"] = trades["data"]["fpmmTrades"][0][
            "creationTimestamp"
        ]

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            query = json_body["query"]
            if "fpmmTrades" in query:
                return trades
            if "token(id" in query:
                return token
            raise AssertionError(f"unexpected Omen query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with self.assertRaisesRegex(MarketConfigurationError, "same-second trades"):
            adapter.list_candles(f"{OMEN_FPMM_ID}:0", resolution="1h")

    def test_omen_trade_history_fails_closed_without_indexed_token_scale(self) -> None:
        adapter = self.make_omen()
        trades = load_fixture("omen", "trades")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            query = json_body["query"]
            if "fpmmTrades" in query:
                return trades
            if "token(id" in query:
                return {"data": {"token": None}}
            raise AssertionError(f"unexpected Omen query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with self.assertRaisesRegex(MarketConfigurationError, "token scale was not indexed"):
            adapter.list_trades(f"{OMEN_FPMM_ID}:0", limit=1)

    def test_omen_signed_transaction_forwarding_is_fail_closed(self) -> None:
        adapter = self.make_omen(
            {
                "omen_rpc_url": "https://rpc.example.test/omen",
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "omen_submit_signed_transactions": True,
            }
        )
        runtime_calls = []

        def unexpected_runtime_call(*args, **kwargs):
            runtime_calls.append((args, kwargs))
            raise AssertionError("fail-closed Omen live trading reached the runtime")

        adapter.runtime.request_json = unexpected_runtime_call  # type: ignore[method-assign]
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="omen",
                    contract_id=f"{OMEN_FPMM_ID}:0",
                    side="BUY",
                    size=1,
                    metadata={
                        "signed_transaction": "0x" + "cd" * 96,
                        "transaction_to": OMEN_FPMM_ID,
                        "method": "buy",
                        "outcome_index": 0,
                        "data": "0x12345678",
                    },
                )
            )
        self.assertEqual(runtime_calls, [])

    def test_gnosis_prediction_markets_alias_uses_official_omen_schema(self) -> None:
        adapter = self.make_gnosis()
        health = adapter.health_check()
        events = adapter.list_events("gnosis", limit=10)
        contracts = adapter.list_contracts(OMEN_FPMM_ID)
        price = adapter.get_price(f"{OMEN_FPMM_ID}:0")
        trades = adapter.list_trades(f"{OMEN_FPMM_ID}:0", limit=2)
        candles = adapter.list_candles(f"{OMEN_FPMM_ID}:0", resolution="1h")
        activities = adapter.list_activity(
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            limit=1,
        )
        recovered = adapter.account_recovery(
            "activity",
            wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            limit=1,
        )
        activity_preview = adapter.copy_trade_from_activity(activities[0])
        paper = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="gnosis_prediction_markets",
                contract_id=f"{OMEN_FPMM_ID}:0",
                side="BUY",
                size=2,
            )
        )

        self.assertEqual(adapter.market_id, "gnosis_prediction_markets")
        self.assertEqual(health["alias_of"], "omen")
        self.assertTrue(health["graphql_url_source"].startswith("config"))
        self.assertEqual(len(events), 1)
        self.assertEqual(len(contracts), 2)
        self.assertAlmostEqual(price.last or 0, 0.62)
        self.assertEqual([trade.trade_id for trade in trades], ["0xgnosistrade2", "0xgnosistrade1"])
        self.assertTrue(all(trade.market_id == "gnosis_prediction_markets" for trade in trades))
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].market_id, "gnosis_prediction_markets")
        self.assertTrue(paper.accepted)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["market_id"], "gnosis_prediction_markets")
        self.assertEqual(recovered["wallet"], "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertEqual(recovered["activity"], activities)
        self.assertTrue(activity_preview.accepted)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{OMEN_FPMM_ID}:0")
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="gnosis_prediction_markets",
                    contract_id=f"{OMEN_FPMM_ID}:0",
                    side="BUY",
                    size=1,
                )
            )

    def test_zeitgeist_uses_official_indexer_shape_for_prices_and_paper_orders(self) -> None:
        adapter = self.make_zeitgeist()
        health = adapter.health_check()

        self.assertTrue(health["indexer_url_configured"])
        self.assertEqual(health["indexer_url_source"], "default")

        events = adapter.list_events("dex", limit=5)
        contracts = adapter.list_contracts(ZEITGEIST_MARKET_ID)
        price = adapter.get_price(f"{ZEITGEIST_MARKET_ID}:0")
        paper = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="zeitgeist",
                contract_id=f"{ZEITGEIST_MARKET_ID}:0",
                side="SELL",
                size=3,
                limit_price=0.8,
            )
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, ZEITGEIST_MARKET_ID)
        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertAlmostEqual(price.last or 0, 0.8076745721806113)
        self.assertTrue(paper.accepted)
        self.assertEqual(paper.average_price, 0.8)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook(f"{ZEITGEIST_MARKET_ID}:0")

        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(market_id="zeitgeist", contract_id=f"{ZEITGEIST_MARKET_ID}:0", side="BUY", size=1)
            )

    def test_zeitgeist_signed_extrinsic_forwarding_is_fail_closed(self) -> None:
        adapter = self.make_zeitgeist(
            {
                "zeitgeist_rpc_url": "https://rpc.example.test/zeitgeist",
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "zeitgeist_submit_signed_extrinsics": True,
            }
        )
        runtime_calls = []

        def unexpected_runtime_call(*args, **kwargs):
            runtime_calls.append((args, kwargs))
            raise AssertionError("fail-closed Zeitgeist live trading reached the runtime")

        adapter.runtime.request_json = unexpected_runtime_call  # type: ignore[method-assign]
        with self.assertRaises(UnsupportedFeatureError):
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="zeitgeist",
                    contract_id=f"{ZEITGEIST_MARKET_ID}:0",
                    side="BUY",
                    size=1,
                    metadata={
                        "pallet": "HybridRouter",
                        "call": "buy",
                        "market_id": 90,
                        "outcome_index": 0,
                        "asset": "CategoricalOutcome:90:0",
                        "asset_count": 2,
                        "amount_in": "1000000",
                        "max_price": "900000",
                        "orders": [1, 3],
                        "strategy": "ImmediateOrCancel",
                        "runtime_spec_version": 57,
                        "signed_extrinsic": "0x" + "ab" * 128,
                    },
                )
            )
        self.assertEqual(runtime_calls, [])

    def test_zeitgeist_sdk_markets_alias_uses_explicit_indexer_configuration(self) -> None:
        adapter = ZeitgeistSdkMarketsAdapter({"zeitgeist_sdk_indexer_url": "https://example.test/zeitgeist-sdk"})
        markets = load_fixture("zeitgeist_sdk_markets", "markets")
        market = load_fixture("zeitgeist_sdk_markets", "market")
        assets = load_fixture("zeitgeist_sdk_markets", "assets")

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://example.test/zeitgeist-sdk")
            query = json_body["query"]
            if "ZeitgeistMarkets" in query:
                return markets
            if "ZeitgeistMarket" in query:
                self.assertEqual(json_body["variables"]["marketId"], 90)
                return market
            if "ZeitgeistAsset" in query:
                self.assertEqual(json_body["variables"]["assetId"], "CategoricalOutcome:90:0")
                return assets
            raise AssertionError(f"unexpected Zeitgeist SDK query: {query}")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        events = adapter.list_events("sdk")
        contracts = adapter.list_contracts("90")
        price = adapter.get_price("90:0")
        paper = adapter.place_paper_order(PaperOrderRequest("zeitgeist_sdk_markets", "90:0", "BUY", 2))

        self.assertEqual(adapter.market_id, "zeitgeist_sdk_markets")
        self.assertEqual(events[0].event_id, "90")
        self.assertEqual(contracts[0].outcome, "Yes")
        self.assertAlmostEqual(price.last or 0, 0.8076745721806113)
        self.assertTrue(paper.accepted)

    def test_zeitgeist_prediction_pools_requires_pool_metadata(self) -> None:
        adapter = self.make_zeitgeist_pools()
        health = adapter.health_check()
        events = adapter.list_events("dex", limit=10)
        contracts = adapter.list_contracts(ZEITGEIST_MARKET_ID)
        price = adapter.get_price(f"{ZEITGEIST_MARKET_ID}:0")
        paper = adapter.place_paper_order(PaperOrderRequest("zeitgeist_prediction_pools", "90:0", "BUY", 2))

        self.assertEqual(adapter.market_id, "zeitgeist_prediction_pools")
        self.assertEqual(health["alias_of"], "zeitgeist")
        self.assertTrue(health["pool_schema_supported"])
        self.assertFalse(health["pool_settlement_supported"])
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertEqual(events[0].raw["pool"]["poolId"], 17)
        self.assertEqual(contracts[0].raw["market"]["pool"]["poolId"], 17)
        self.assertAlmostEqual(price.last or 0, 0.8076745721806113)
        self.assertTrue(paper.accepted)

        with self.assertRaises(UnsupportedFeatureError):
            adapter.get_orderbook("90:0")


if __name__ == "__main__":
    unittest.main()
