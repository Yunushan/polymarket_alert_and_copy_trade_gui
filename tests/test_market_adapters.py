from __future__ import annotations

import unittest

from market_adapters import (
    MARKET_CATALOG,
    MARKET_IDS,
    AdapterRegistry,
    AugurAdapter,
    AzuroAdapter,
    BetMGMAdapter,
    BetfairExchangeAdapter,
    BlinqAdapter,
    CoinbasePredictionMarketsAdapter,
    CryptoComPredictAdapter,
    DraftKingsPredictionsAdapter,
    FanaticsMarketsAdapter,
    FanDuelPredictsAdapter,
    ContextV2Adapter,
    DFlowAdapter,
    DriftBetAdapter,
    FrenzyFinanceAdapter,
    GeminiPredictionAdapter,
    GoodJudgmentOpenAdapter,
    GnosisPredictionMarketsAdapter,
    HyperliquidAdapter,
    HedgehogMarketsAdapter,
    IBKRForecastTraderAdapter,
    IowaElectronicMarketsAdapter,
    ForecastExAdapter,
    CMEPredictionMarketsAdapter,
    KalshiAdapter,
    KalshiViaRobinhoodAdapter,
    LimitlessAdapter,
    ManifoldAdapter,
    MatchbookAdapter,
    MarketAdapter,
    MarketCapabilities,
    MarketMetadata,
    PaperOrderRequest,
    PaperOrderResult,
    PredictItAdapter,
    PredictFunAdapter,
    ProbableAdapter,
    PRDTFinanceAdapter,
    ProphetExchangeAdapter,
    OmenAdapter,
    RealityEthMarketsAdapter,
    SxBetAdapter,
    SmarketsAdapter,
    SeerAdapter,
    SciCastAdapter,
    SpaceAdapter,
    ThalesMarketAdapter,
    TrueoAdapter,
    ZetariumWorldAdapter,
    LamasFinanceAdapter,
    StubMarketAdapter,
    UnsupportedFeatureError,
    MetaculusAdapter,
    MetaDAOAdapter,
    MyriadAdapter,
    NadexAdapter,
    OpinionAdapter,
    RobinhoodPredictionMarketsAdapter,
    VERIFIED_BLOCKERS,
    VerifiedBlockedAdapter,
    XOMarketAdapter,
    XMarketAdapter,
    ZeitgeistAdapter,
    ZeitgeistPredictionPoolsAdapter,
    build_default_registry,
    create_stub_adapter,
)
from market_adapters.errors import MarketConfigurationError


CAPABILITY_KEYS = {
    "market_discovery",
    "event_listing",
    "price_reading",
    "orderbook_reading",
    "trade_history",
    "candle_history",
    "alerts",
    "paper_trading",
    "live_trading",
    "copy_trading",
    "api_required",
    "credentials_required",
    "kyc_required",
    "region_limited",
}

IMPLEMENTED_MARKETS = {
    "betmgm",
    "polymarket",
    "blinq",
    "coinbase_prediction_markets",
    "robinhood_prediction_markets",
    "kalshi_via_robinhood",
    "kalshi",
    "predictit",
    "manifold",
    "metaculus",
    "good_judgment_open",
    "limitless_exchange",
    "sx_bet",
    "azuro",
    "augur",
    "omen",
    "gnosis_prediction_markets",
    "zeitgeist",
    "myriad_markets",
    "xo_market",
    "opinion_labs",
    "gemini_titan",
    "predict_fun",
    "betfair_exchange",
    "crypto_com_predict",
    "draftkings_predictions",
    "fanatics_markets",
    "fanduel_predicts",
    "context_v2",
    "smarkets",
    "thales_market",
    "metadao",
    "seer",
    "hyperliquid",
    "trueo",
    "zeitgeist_sdk_markets",
    "zeitgeist_prediction_pools",
    "reality_eth_markets",
    "ibkr_forecasttrader",
    "forecastex",
    "cme_prediction_markets",
    "dflow",
    "drift_bet",
    "frenzy_finance",
    "space",
    "hedgehog_markets",
    "xmarket",
    "probable",
    "matchbook",
    "prophet_exchange",
    "prdt_finance",
    "zetarium_world",
    "lamas_finance",
    "nadex",
    "iowa_electronic_markets",
    "scicast",
}
VERIFIED_BLOCKED_MARKETS = set(VERIFIED_BLOCKERS)

HISTORY_CAPABILITIES = {
    "polymarket": {"trade_history", "candle_history"},
    "blinq": {"trade_history", "candle_history"},
    "coinbase_prediction_markets": {"trade_history", "candle_history"},
    "kalshi": {"trade_history", "candle_history"},
    "robinhood_prediction_markets": {"trade_history", "candle_history"},
    "kalshi_via_robinhood": {"trade_history", "candle_history"},
    "manifold": {"trade_history", "candle_history"},
    "metaculus": {"candle_history"},
    "good_judgment_open": {"candle_history"},
    "limitless_exchange": {"trade_history", "candle_history"},
    "hyperliquid": {"trade_history", "candle_history"},
    "ibkr_forecasttrader": {"trade_history", "candle_history"},
    "forecastex": {"trade_history", "candle_history"},
    "cme_prediction_markets": {"trade_history", "candle_history"},
    "myriad_markets": {"trade_history", "candle_history"},
    "opinion_labs": {"trade_history", "candle_history"},
    "betfair_exchange": {"trade_history", "candle_history"},
    "matchbook": {"trade_history", "candle_history"},
    "sx_bet": {"trade_history", "candle_history"},
    "gemini_titan": {"trade_history", "candle_history"},
    "iowa_electronic_markets": {"candle_history"},
    "space": {"trade_history", "candle_history"},
    "scicast": {"trade_history", "candle_history"},
    "probable": {"trade_history", "candle_history"},
    "context_v2": {"trade_history", "candle_history"},
    "predict_fun": {"trade_history", "candle_history"},
    "dflow": {"trade_history", "candle_history"},
    "xo_market": {"trade_history", "candle_history"},
}


class DummyAdapter(MarketAdapter):
    metadata = MarketMetadata(
        market_id="dummy",
        display_name="Dummy",
        capabilities=MarketCapabilities(event_listing=True, paper_trading=True),
    )

    def list_events(self, query: str = "", limit: int = 50):
        self.ensure_capability("event_listing")
        return []

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        return PaperOrderResult(
            market_id=order.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message="accepted",
            filled_size=order.size,
            average_price=order.limit_price,
        )


class AdapterFoundationTests(unittest.TestCase):
    def test_catalog_contains_goal_markets_with_unique_ids(self) -> None:
        self.assertEqual(len(MARKET_IDS), len(set(MARKET_IDS)))
        self.assertEqual(len(MARKET_CATALOG), 68)
        self.assertIn("polymarket", MARKET_IDS)
        self.assertIn("kalshi", MARKET_IDS)
        self.assertIn("limitless_exchange", MARKET_IDS)
        self.assertIn("fanatics_markets", MARKET_IDS)
        self.assertIn("hyperliquid", MARKET_IDS)
        self.assertIn("betfair_exchange", MARKET_IDS)
        self.assertIn("underdog_sports", MARKET_IDS)

    def test_default_registry_exposes_catalog_metadata_and_implemented_adapters(self) -> None:
        registry = build_default_registry()

        self.assertEqual(set(registry.list_market_ids()), set(MARKET_IDS))
        self.assertTrue(all(registry.has_adapter(market_id) for market_id in MARKET_IDS))
        self.assertEqual(registry.get_metadata("polymarket").display_name, "Polymarket")
        self.assertEqual(registry.get_metadata("betmgm").display_name, "BetMGM")
        self.assertIsInstance(registry.create("betmgm"), BetMGMAdapter)
        self.assertEqual(registry.create("polymarket").market_id, "polymarket")
        self.assertEqual(registry.get_metadata("blinq").display_name, "Blinq")
        self.assertIsInstance(registry.create("blinq"), BlinqAdapter)
        self.assertEqual(registry.get_metadata("kalshi").display_name, "Kalshi")
        self.assertIsInstance(registry.create("kalshi"), KalshiAdapter)
        self.assertEqual(registry.get_metadata("predictit").display_name, "PredictIt")
        self.assertIsInstance(registry.create("predictit"), PredictItAdapter)
        self.assertEqual(registry.get_metadata("crypto_com_predict").display_name, "Crypto.com Predict / CDNA")
        self.assertIsInstance(registry.create("crypto_com_predict"), CryptoComPredictAdapter)
        self.assertEqual(registry.get_metadata("fanatics_markets").display_name, "Fanatics Markets")
        self.assertIsInstance(registry.create("fanatics_markets"), FanaticsMarketsAdapter)
        self.assertEqual(registry.get_metadata("nadex").display_name, "Nadex")
        self.assertIsInstance(registry.create("nadex"), NadexAdapter)
        self.assertIsInstance(registry.create("iowa_electronic_markets"), IowaElectronicMarketsAdapter)
        self.assertEqual(registry.get_metadata("scicast").display_name, "SciCast")
        self.assertIsInstance(registry.create("scicast"), SciCastAdapter)
        self.assertEqual(registry.get_metadata("fanduel_predicts").display_name, "FanDuel Predicts")
        self.assertIsInstance(registry.create("fanduel_predicts"), FanDuelPredictsAdapter)
        self.assertEqual(registry.get_metadata("coinbase_prediction_markets").display_name, "Coinbase Prediction Markets")
        self.assertIsInstance(registry.create("coinbase_prediction_markets"), CoinbasePredictionMarketsAdapter)
        self.assertEqual(registry.get_metadata("robinhood_prediction_markets").display_name, "Robinhood Prediction Markets")
        self.assertIsInstance(registry.create("robinhood_prediction_markets"), RobinhoodPredictionMarketsAdapter)
        self.assertIsInstance(registry.create("kalshi_via_robinhood"), KalshiViaRobinhoodAdapter)
        self.assertEqual(registry.get_metadata("draftkings_predictions").display_name, "DraftKings Predictions")
        self.assertIsInstance(registry.create("draftkings_predictions"), DraftKingsPredictionsAdapter)
        self.assertEqual(registry.get_metadata("manifold").display_name, "Manifold Markets")
        self.assertIsInstance(registry.create("manifold"), ManifoldAdapter)
        self.assertEqual(registry.get_metadata("metaculus").display_name, "Metaculus")
        self.assertIsInstance(registry.create("metaculus"), MetaculusAdapter)
        self.assertEqual(registry.get_metadata("good_judgment_open").display_name, "Good Judgment Open")
        self.assertIsInstance(registry.create("good_judgment_open"), GoodJudgmentOpenAdapter)
        self.assertEqual(registry.get_metadata("limitless_exchange").display_name, "Limitless Exchange")
        self.assertIsInstance(registry.create("limitless_exchange"), LimitlessAdapter)
        self.assertEqual(registry.get_metadata("sx_bet").display_name, "SX Bet / SX Network")
        self.assertIsInstance(registry.create("sx_bet"), SxBetAdapter)
        self.assertEqual(registry.get_metadata("azuro").display_name, "Azuro")
        self.assertIsInstance(registry.create("azuro"), AzuroAdapter)
        self.assertEqual(registry.get_metadata("augur").display_name, "Augur")
        self.assertIsInstance(registry.create("augur"), AugurAdapter)
        self.assertEqual(registry.get_metadata("omen").display_name, "Omen")
        self.assertIsInstance(registry.create("omen"), OmenAdapter)
        self.assertEqual(registry.get_metadata("gnosis_prediction_markets").display_name, "Gnosis Prediction Markets")
        self.assertIsInstance(registry.create("gnosis_prediction_markets"), GnosisPredictionMarketsAdapter)
        self.assertEqual(registry.get_metadata("zeitgeist").display_name, "Zeitgeist")
        self.assertEqual(registry.get_metadata("reality_eth_markets").display_name, "Reality.eth Markets")
        self.assertIsInstance(registry.create("reality_eth_markets"), RealityEthMarketsAdapter)
        self.assertEqual(registry.get_metadata("drift_bet").display_name, "Drift BET")
        self.assertIsInstance(registry.create("drift_bet"), DriftBetAdapter)
        self.assertEqual(registry.get_metadata("space").display_name, "Space")
        self.assertIsInstance(registry.create("space"), SpaceAdapter)
        self.assertEqual(registry.get_metadata("hedgehog_markets").display_name, "Hedgehog Markets")
        self.assertIsInstance(registry.create("hedgehog_markets"), HedgehogMarketsAdapter)
        self.assertEqual(registry.get_metadata("frenzy_finance").display_name, "Frenzy Finance")
        self.assertIsInstance(registry.create("frenzy_finance"), FrenzyFinanceAdapter)
        self.assertIsInstance(registry.create("zeitgeist"), ZeitgeistAdapter)
        self.assertEqual(registry.get_metadata("zeitgeist_prediction_pools").display_name, "Zeitgeist Prediction Pools")
        self.assertIsInstance(registry.create("zeitgeist_prediction_pools"), ZeitgeistPredictionPoolsAdapter)
        self.assertEqual(registry.get_metadata("myriad_markets").display_name, "Myriad Markets")
        self.assertIsInstance(registry.create("myriad_markets"), MyriadAdapter)
        self.assertEqual(registry.get_metadata("xo_market").display_name, "XO Market")
        self.assertIsInstance(registry.create("xo_market"), XOMarketAdapter)
        self.assertEqual(registry.get_metadata("opinion_labs").display_name, "Opinion Labs")
        self.assertIsInstance(registry.create("opinion_labs"), OpinionAdapter)
        self.assertEqual(registry.get_metadata("gemini_titan").display_name, "Gemini Titan / Gemini Predictions")
        self.assertIsInstance(registry.create("gemini_titan"), GeminiPredictionAdapter)
        self.assertEqual(registry.get_metadata("predict_fun").display_name, "Predict.fun")
        self.assertIsInstance(registry.create("predict_fun"), PredictFunAdapter)
        self.assertEqual(registry.get_metadata("betfair_exchange").display_name, "Betfair Exchange")
        self.assertIsInstance(registry.create("betfair_exchange"), BetfairExchangeAdapter)
        self.assertEqual(registry.get_metadata("xmarket").display_name, "Xmarket")
        self.assertIsInstance(registry.create("xmarket"), XMarketAdapter)
        self.assertEqual(registry.get_metadata("probable").display_name, "Probable")
        self.assertIsInstance(registry.create("probable"), ProbableAdapter)
        self.assertEqual(registry.get_metadata("matchbook").display_name, "Matchbook")
        self.assertIsInstance(registry.create("matchbook"), MatchbookAdapter)
        self.assertEqual(registry.get_metadata("prophet_exchange").display_name, "Prophet Exchange")
        self.assertIsInstance(registry.create("prophet_exchange"), ProphetExchangeAdapter)
        self.assertEqual(registry.get_metadata("prdt_finance").display_name, "PRDT Finance")
        self.assertIsInstance(registry.create("prdt_finance"), PRDTFinanceAdapter)
        self.assertEqual(registry.get_metadata("dflow").display_name, "DFlow")
        self.assertIsInstance(registry.create("dflow"), DFlowAdapter)
        self.assertEqual(registry.get_metadata("context_v2").display_name, "Context V2")
        self.assertIsInstance(registry.create("context_v2"), ContextV2Adapter)
        self.assertEqual(registry.get_metadata("smarkets").display_name, "Smarkets")
        self.assertIsInstance(registry.create("smarkets"), SmarketsAdapter)
        self.assertEqual(registry.get_metadata("thales_market").display_name, "Thales Market")
        self.assertIsInstance(registry.create("thales_market"), ThalesMarketAdapter)
        self.assertEqual(registry.get_metadata("metadao").display_name, "MetaDAO")
        self.assertIsInstance(registry.create("metadao"), MetaDAOAdapter)
        self.assertEqual(registry.get_metadata("seer").display_name, "Seer")
        self.assertIsInstance(registry.create("seer"), SeerAdapter)
        self.assertEqual(registry.get_metadata("hyperliquid").display_name, "Hyperliquid")
        self.assertIsInstance(registry.create("hyperliquid"), HyperliquidAdapter)
        self.assertEqual(registry.get_metadata("trueo").display_name, "Trueo")
        self.assertIsInstance(registry.create("trueo"), TrueoAdapter)
        self.assertEqual(registry.get_metadata("zetarium_world").display_name, "Zetarium World")
        self.assertIsInstance(registry.create("zetarium_world"), ZetariumWorldAdapter)
        self.assertEqual(registry.get_metadata("lamas_finance").display_name, "Lamas Finance")
        self.assertIsInstance(registry.create("lamas_finance"), LamasFinanceAdapter)
        self.assertIsInstance(registry.create("ibkr_forecasttrader"), IBKRForecastTraderAdapter)
        self.assertIsInstance(registry.create("forecastex"), ForecastExAdapter)
        self.assertIsInstance(registry.create("cme_prediction_markets"), CMEPredictionMarketsAdapter)

    def test_non_implemented_catalog_entries_create_stub_adapters(self) -> None:
        registry = build_default_registry()

        for market_id in MARKET_IDS:
            if market_id in IMPLEMENTED_MARKETS:
                continue
            adapter = registry.create(market_id)
            self.assertIsInstance(adapter, StubMarketAdapter)
            self.assertEqual(adapter.market_id, market_id)
            self.assertFalse(adapter.health_check()["ok"])

    def test_verified_blocked_markets_have_specific_health_and_errors(self) -> None:
        registry = build_default_registry()

        for market_id in VERIFIED_BLOCKED_MARKETS:
            with self.subTest(market_id=market_id):
                adapter = registry.create(market_id)
                health = adapter.health_check()

                self.assertIsInstance(adapter, VerifiedBlockedAdapter)
                self.assertTrue(health["stub"])
                self.assertTrue(health["verified_blocker"])
                expected_review = str(VERIFIED_BLOCKERS[market_id].get("last_reviewed") or "2026-05-26")
                self.assertEqual(health["last_reviewed"], expected_review)
                self.assertGreaterEqual(len(health["references"]), 1)
                self.assertIn(f"Verified {expected_review}", health["message"])

                with self.assertRaises(UnsupportedFeatureError) as ctx:
                    adapter.list_events()
                self.assertEqual(ctx.exception.market_id, market_id)
                self.assertEqual(ctx.exception.feature, "event_listing")
                self.assertIn("verified blocked", str(ctx.exception))

    def test_catalog_stub_markets_do_not_advertise_working_capabilities(self) -> None:
        registry = build_default_registry()

        for market_id in MARKET_IDS:
            metadata = registry.get_metadata(market_id)
            if market_id in IMPLEMENTED_MARKETS:
                self.assertTrue(any(metadata.capabilities.to_dict().values()))
                continue
            self.assertEqual(metadata.capabilities.to_dict(), {key: False for key in CAPABILITY_KEYS})

    def test_history_capability_flags_match_adapters_with_documented_feeds(self) -> None:
        registry = build_default_registry()

        for market_id, expected in HISTORY_CAPABILITIES.items():
            with self.subTest(market_id=market_id):
                capabilities = registry.create(market_id).capabilities.to_dict()
                self.assertEqual(
                    {name for name in ("trade_history", "candle_history") if capabilities[name]},
                    expected,
                )

        for market_id in set(MARKET_IDS) - set(HISTORY_CAPABILITIES):
            with self.subTest(market_id=market_id):
                capabilities = registry.create(market_id).capabilities.to_dict()
                self.assertFalse(capabilities["trade_history"])
                self.assertFalse(capabilities["candle_history"])

    def test_all_default_adapters_satisfy_basic_contract(self) -> None:
        registry = build_default_registry()

        for market_id in MARKET_IDS:
            adapter = registry.create(market_id)
            health = adapter.health_check()

            self.assertEqual(adapter.metadata.market_id, market_id)
            self.assertEqual(adapter.market_id, market_id)
            self.assertTrue(adapter.display_name)
            self.assertEqual(set(adapter.capabilities.to_dict()), CAPABILITY_KEYS)
            self.assertEqual(health["market_id"], market_id)
            self.assertIn("ok", health)
            self.assertIn("message", health)

    def test_stub_adapters_reject_all_operational_methods(self) -> None:
        registry = build_default_registry()
        order = PaperOrderRequest(
            market_id="stub-market",
            contract_id="contract-1",
            side="BUY",
            size=1.0,
            limit_price=0.5,
        )

        for market_id in MARKET_IDS:
            if market_id in IMPLEMENTED_MARKETS:
                continue
            adapter = registry.create(market_id)

            operations = (
                ("event_listing", lambda adapter=adapter: adapter.list_events()),
                ("event_listing", lambda adapter=adapter: adapter.list_contracts("event-1")),
                ("price_reading", lambda adapter=adapter: adapter.get_price("contract-1")),
                ("orderbook_reading", lambda adapter=adapter: adapter.get_orderbook("contract-1")),
                ("trade_history", lambda adapter=adapter: adapter.list_trades("contract-1")),
                ("candle_history", lambda adapter=adapter: adapter.list_candles("contract-1")),
                ("paper_trading", lambda adapter=adapter: adapter.place_paper_order(order)),
                ("live_trading", lambda adapter=adapter: adapter.place_live_order(order)),
                ("copy_trading", lambda adapter=adapter: adapter.copy_trade_from_activity({})),
            )

            for feature, operation in operations:
                with self.subTest(market_id=market_id, feature=feature):
                    with self.assertRaises(UnsupportedFeatureError) as ctx:
                        operation()
                    self.assertEqual(ctx.exception.market_id, market_id)
                    self.assertEqual(ctx.exception.feature, feature)

    def test_stub_adapter_raises_market_specific_unsupported_errors(self) -> None:
        adapter = create_stub_adapter(
            MarketMetadata(market_id="custom_stub", display_name="Custom Stub")
        )

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_price("contract-1")

        self.assertEqual(ctx.exception.market_id, "custom_stub")
        self.assertEqual(ctx.exception.feature, "price_reading")
        self.assertIn("Custom Stub", str(ctx.exception))
        self.assertIn("official adapter", str(ctx.exception))
        self.assertIn("not been implemented", str(ctx.exception))

        with self.assertRaises(UnsupportedFeatureError) as history_ctx:
            adapter.list_trades("contract-1")

        self.assertEqual(history_ctx.exception.market_id, "custom_stub")
        self.assertEqual(history_ctx.exception.feature, "trade_history")
        self.assertIn("Custom Stub", str(history_ctx.exception))

        with self.assertRaises(UnsupportedFeatureError) as candle_ctx:
            adapter.list_candles("contract-1")

        self.assertEqual(candle_ctx.exception.market_id, "custom_stub")
        self.assertEqual(candle_ctx.exception.feature, "candle_history")
        self.assertIn("Custom Stub", str(candle_ctx.exception))

    def test_registry_registers_adapter_and_creates_configured_instance(self) -> None:
        registry = AdapterRegistry()
        registry.register_adapter(DummyAdapter)

        adapter = registry.create("dummy", {"enabled": True})
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="dummy",
                contract_id="contract-1",
                side="BUY",
                size=3.0,
                limit_price=0.42,
            )
        )

        self.assertTrue(registry.has_adapter("dummy"))
        self.assertEqual(adapter.config, {"enabled": True})
        self.assertTrue(result.accepted)
        self.assertEqual(result.filled_size, 3.0)
        self.assertEqual(result.average_price, 0.42)

    def test_registry_rejects_duplicate_adapter_registration(self) -> None:
        registry = AdapterRegistry()
        registry.register_adapter(DummyAdapter)

        with self.assertRaises(MarketConfigurationError):
            registry.register_adapter(DummyAdapter)

    def test_unsupported_feature_error_is_clear(self) -> None:
        adapter = MarketAdapter()

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_price("contract-1")

        self.assertEqual(ctx.exception.market_id, "base")
        self.assertEqual(ctx.exception.feature, "price_reading")
        self.assertIn("price_reading", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
