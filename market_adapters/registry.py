from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Type

from .base import MarketAdapter
from .catalog import MARKET_CATALOG
from .errors import MarketConfigurationError
from .expanded_catalog import EXPANDED_VERIFIED_BLOCKERS
from .types import MarketMetadata


VERIFIED_BLOCKERS: Dict[str, Dict[str, Any]] = {
    "fact_machine": {
        "reason": (
            "Verified 2026-08-24: Fact Machine's public domains still do not publish a documented API/SDK or stable "
            "protocol integration contract suitable for discovery, pricing, or order automation in this app."
        ),
        "references": [
            "https://factmachine.io",
            "https://factmachine.com",
        ],
        "last_reviewed": "2026-08-24",
    },
    "infer": {
        "reason": (
            "Verified 2026-05-26; re-verified 2026-08-17: INFER-pub now redirects to the RAND Forecasting Initiative "
            "permanent read-only archive. Accounts, forecasting, comments, and live submission are no longer "
            "available; the archive exposes preserved questions/leaderboards but no supported API or export contract "
            "for this app. The adapter must not scrape pages or automate private sessions."
        ),
        "references": [
            "https://www.infer-pub.com",
            "https://www.randforecastinginitiative.org",
            "https://www.randforecastinginitiative.org/questions",
        ],
    },
    "prizepicks": {
        "reason": (
            "Verified 2026-08-23: PrizePicks documents that its event contracts are provided by Kalshi, but "
            "neither party publishes a machine-readable mapping from the venue-wide Kalshi catalog to the "
            "current PrizePicks board. Relabeling every Kalshi contract as PrizePicks would overstate support; "
            "PrizePicks also publishes no account/order automation API for this app."
        ),
        "references": [
            "https://www.prizepicks.com/press-news/prizepicks-launches-prediction-markets-offering-with-kalshi",
            "https://www.prizepicks.com/playbook-article/prediction-markets-vs-sports-betting-differences-explained",
            "https://docs.kalshi.com/getting_started/quick_start_market_data",
        ],
        "last_reviewed": "2026-08-23",
    },
    "underdog_sports": {
        "reason": (
            "Verified 2026-08-23: Underdog identifies CDNA as its event-contract exchange, but no official "
            "machine-readable mapping from the venue-wide CDNA catalog to the current Underdog board is "
            "published. Relabeling every CDNA contract as Underdog would overstate support; no Underdog "
            "account/order automation API is published for this app."
        ),
        "references": [
            "https://legal.underdogsports.com/",
            "https://data.crypto.com/docs",
            "https://data.crypto.com/quickstart",
        ],
        "last_reviewed": "2026-08-23",
    },
    "probo": {
        "reason": (
            "Verified 2026-08-17: Probo's official site states that operations are closed; the product also "
            "does not publish a public official API, SDK, or automation permission flow suitable for this app."
        ),
        "references": [
            "https://probo.in",
        ],
        "last_reviewed": "2026-08-17",
    },
}

VERIFIED_BLOCKERS.update(EXPANDED_VERIFIED_BLOCKERS)


AdapterFactory = Callable[[Optional[Mapping[str, Any]]], MarketAdapter]


class AdapterRegistry:
    """Registry for market adapter factories."""

    def __init__(self) -> None:
        self._metadata: Dict[str, MarketMetadata] = {}
        self._factories: Dict[str, AdapterFactory] = {}

    def register_metadata(self, metadata: MarketMetadata, *, replace: bool = False) -> None:
        market_id = self._normalize_market_id(metadata.market_id)
        if not replace and market_id in self._metadata:
            raise MarketConfigurationError(f"Market metadata already registered: {market_id}")
        self._metadata[market_id] = metadata

    def register_adapter(self, adapter_cls: Type[MarketAdapter], *, replace: bool = False) -> None:
        metadata = adapter_cls.metadata
        market_id = self._normalize_market_id(metadata.market_id)
        if market_id == "base":
            raise MarketConfigurationError("Base MarketAdapter cannot be registered directly.")
        if not replace and market_id in self._factories:
            raise MarketConfigurationError(f"Adapter already registered: {market_id}")
        self.register_metadata(metadata, replace=True)
        self._factories[market_id] = adapter_cls

    def register_factory(
        self,
        metadata: MarketMetadata,
        factory: AdapterFactory,
        *,
        replace: bool = False,
    ) -> None:
        market_id = self._normalize_market_id(metadata.market_id)
        if not replace and market_id in self._factories:
            raise MarketConfigurationError(f"Adapter already registered: {market_id}")
        self.register_metadata(metadata, replace=True)
        self._factories[market_id] = factory

    def create(self, market_id: str, config: Optional[Mapping[str, Any]] = None) -> MarketAdapter:
        normalized = self._normalize_market_id(market_id)
        factory = self._factories.get(normalized)
        if factory is None:
            raise MarketConfigurationError(f"No adapter registered for market: {normalized}")
        return factory(config)

    def get_metadata(self, market_id: str) -> MarketMetadata:
        normalized = self._normalize_market_id(market_id)
        try:
            return self._metadata[normalized]
        except KeyError as exc:
            raise MarketConfigurationError(f"Unknown market: {normalized}") from exc

    def list_metadata(self, *, enabled_ids: Optional[Mapping[str, bool]] = None) -> List[MarketMetadata]:
        metadata = list(self._metadata.values())
        metadata.sort(key=lambda item: item.display_name.lower())
        if enabled_ids is None:
            return metadata
        return [m for m in metadata if enabled_ids.get(m.market_id, False)]

    def list_market_ids(self) -> List[str]:
        return sorted(self._metadata)

    def has_adapter(self, market_id: str) -> bool:
        return self._normalize_market_id(market_id) in self._factories

    @staticmethod
    def _normalize_market_id(market_id: str) -> str:
        normalized = str(market_id or "").strip().lower()
        if not normalized:
            raise MarketConfigurationError("Market id cannot be empty.")
        return normalized


def build_default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for metadata in MARKET_CATALOG:
        registry.register_metadata(metadata)
    from .azuro import AzuroAdapter
    from .betmgm import BetMGMAdapter
    from .betfair import BetfairExchangeAdapter
    from .blinq import BlinqAdapter
    from .coinbase_prediction import CoinbasePredictionMarketsAdapter
    from .crypto_com_predict import CryptoComPredictAdapter, FanaticsMarketsAdapter
    from .draftkings_predictions import DraftKingsPredictionsAdapter
    from .context_v2 import ContextV2Adapter
    from .dflow import DFlowAdapter
    from .drift_bet import DriftBetAdapter
    from .frenzy import FrenzyFinanceAdapter
    from .fanduel_predicts import FanDuelPredictsAdapter
    from .gemini import GeminiPredictionAdapter
    from .good_judgment_open import GoodJudgmentOpenAdapter
    from .hyperliquid import HyperliquidAdapter
    from .hypermind import HypermindAdapter
    from .hedgehog import HedgehogMarketsAdapter
    from .ibkr_event_contracts import CMEPredictionMarketsAdapter, ForecastExAdapter, IBKRForecastTraderAdapter
    from .iowa_electronic_markets import IowaElectronicMarketsAdapter
    from .kalshi import KalshiAdapter
    from .robinhood_prediction import KalshiViaRobinhoodAdapter, RobinhoodPredictionMarketsAdapter
    from .lamas_finance import LamasFinanceAdapter
    from .legacy_web3 import (
        AugurAdapter,
        GnosisPredictionMarketsAdapter,
        OmenAdapter,
        RealityEthMarketsAdapter,
        ZeitgeistAdapter,
        ZeitgeistPredictionPoolsAdapter,
        ZeitgeistSdkMarketsAdapter,
    )
    from .limitless import LimitlessAdapter
    from .matchbook import MatchbookAdapter
    from .manifold import ManifoldAdapter
    from .metaculus import MetaculusAdapter
    from .metadao import MetaDAOAdapter
    from .myriad import MyriadAdapter
    from .nadex import NadexAdapter
    from .opinion import OpinionAdapter
    from .polymarket import PolymarketAdapter
    from .probable import ProbableAdapter
    from .prdt_finance import PRDTFinanceAdapter
    from .prophet_exchange import ProphetExchangeAdapter
    from .predict_fun import PredictFunAdapter
    from .predictit import PredictItAdapter
    from .sx_bet import SxBetAdapter
    from .smarkets import SmarketsAdapter
    from .seer import SeerAdapter
    from .scicast import SciCastAdapter
    from .space import SpaceAdapter
    from .thales import ThalesMarketAdapter
    from .trueo import TrueoAdapter
    from .zetarium import ZetariumWorldAdapter
    from .stub import create_stub_adapter, create_verified_blocked_adapter
    from .xo import XOMarketAdapter
    from .xmarket import XMarketAdapter

    implemented_adapters = (
        BetMGMAdapter,
        PolymarketAdapter,
        BlinqAdapter,
        CoinbasePredictionMarketsAdapter,
        RobinhoodPredictionMarketsAdapter,
        KalshiViaRobinhoodAdapter,
        ProbableAdapter,
        ProphetExchangeAdapter,
        PRDTFinanceAdapter,
        LamasFinanceAdapter,
        KalshiAdapter,
        PredictItAdapter,
        CryptoComPredictAdapter,
        DraftKingsPredictionsAdapter,
        FanaticsMarketsAdapter,
        ContextV2Adapter,
        SmarketsAdapter,
        SeerAdapter,
        ThalesMarketAdapter,
        TrueoAdapter,
        DFlowAdapter,
        DriftBetAdapter,
        FrenzyFinanceAdapter,
        FanDuelPredictsAdapter,
        ManifoldAdapter,
        MetaculusAdapter,
        GoodJudgmentOpenAdapter,
        HypermindAdapter,
        LimitlessAdapter,
        SxBetAdapter,
        AzuroAdapter,
        AugurAdapter,
        OmenAdapter,
        GnosisPredictionMarketsAdapter,
        RealityEthMarketsAdapter,
        ZeitgeistAdapter,
        ZeitgeistPredictionPoolsAdapter,
        ZeitgeistSdkMarketsAdapter,
        GeminiPredictionAdapter,
        HyperliquidAdapter,
        IBKRForecastTraderAdapter,
        ForecastExAdapter,
        CMEPredictionMarketsAdapter,
        IowaElectronicMarketsAdapter,
        MyriadAdapter,
        OpinionAdapter,
        PredictFunAdapter,
        XOMarketAdapter,
        BetfairExchangeAdapter,
        XMarketAdapter,
        MatchbookAdapter,
        MetaDAOAdapter,
        SpaceAdapter,
        HedgehogMarketsAdapter,
        ZetariumWorldAdapter,
        NadexAdapter,
        SciCastAdapter,
    )
    registry.register_adapter(PolymarketAdapter, replace=True)
    registry.register_adapter(BetMGMAdapter, replace=True)
    registry.register_adapter(BlinqAdapter, replace=True)
    registry.register_adapter(CoinbasePredictionMarketsAdapter, replace=True)
    registry.register_adapter(RobinhoodPredictionMarketsAdapter, replace=True)
    registry.register_adapter(KalshiViaRobinhoodAdapter, replace=True)
    registry.register_adapter(ProbableAdapter, replace=True)
    registry.register_adapter(ProphetExchangeAdapter, replace=True)
    registry.register_adapter(PRDTFinanceAdapter, replace=True)
    registry.register_adapter(LamasFinanceAdapter, replace=True)
    registry.register_adapter(KalshiAdapter, replace=True)
    registry.register_adapter(PredictItAdapter, replace=True)
    registry.register_adapter(CryptoComPredictAdapter, replace=True)
    registry.register_adapter(DraftKingsPredictionsAdapter, replace=True)
    registry.register_adapter(FanaticsMarketsAdapter, replace=True)
    registry.register_adapter(ContextV2Adapter, replace=True)
    registry.register_adapter(SmarketsAdapter, replace=True)
    registry.register_adapter(SeerAdapter, replace=True)
    registry.register_adapter(ThalesMarketAdapter, replace=True)
    registry.register_adapter(TrueoAdapter, replace=True)
    registry.register_adapter(DFlowAdapter, replace=True)
    registry.register_adapter(DriftBetAdapter, replace=True)
    registry.register_adapter(FrenzyFinanceAdapter, replace=True)
    registry.register_adapter(FanDuelPredictsAdapter, replace=True)
    registry.register_adapter(ManifoldAdapter, replace=True)
    registry.register_adapter(MetaculusAdapter, replace=True)
    registry.register_adapter(GoodJudgmentOpenAdapter, replace=True)
    registry.register_adapter(HypermindAdapter, replace=True)
    registry.register_adapter(LimitlessAdapter, replace=True)
    registry.register_adapter(SxBetAdapter, replace=True)
    registry.register_adapter(AzuroAdapter, replace=True)
    registry.register_adapter(AugurAdapter, replace=True)
    registry.register_adapter(OmenAdapter, replace=True)
    registry.register_adapter(GnosisPredictionMarketsAdapter, replace=True)
    registry.register_adapter(RealityEthMarketsAdapter, replace=True)
    registry.register_adapter(ZeitgeistAdapter, replace=True)
    registry.register_adapter(ZeitgeistPredictionPoolsAdapter, replace=True)
    registry.register_adapter(ZeitgeistSdkMarketsAdapter, replace=True)
    registry.register_adapter(GeminiPredictionAdapter, replace=True)
    registry.register_adapter(HyperliquidAdapter, replace=True)
    registry.register_adapter(IBKRForecastTraderAdapter, replace=True)
    registry.register_adapter(ForecastExAdapter, replace=True)
    registry.register_adapter(CMEPredictionMarketsAdapter, replace=True)
    registry.register_adapter(IowaElectronicMarketsAdapter, replace=True)
    registry.register_adapter(MyriadAdapter, replace=True)
    registry.register_adapter(OpinionAdapter, replace=True)
    registry.register_adapter(PredictFunAdapter, replace=True)
    registry.register_adapter(XOMarketAdapter, replace=True)
    registry.register_adapter(BetfairExchangeAdapter, replace=True)
    registry.register_adapter(XMarketAdapter, replace=True)
    registry.register_adapter(MatchbookAdapter, replace=True)
    registry.register_adapter(MetaDAOAdapter, replace=True)
    registry.register_adapter(SpaceAdapter, replace=True)
    registry.register_adapter(HedgehogMarketsAdapter, replace=True)
    registry.register_adapter(ZetariumWorldAdapter, replace=True)
    registry.register_adapter(NadexAdapter, replace=True)
    registry.register_adapter(SciCastAdapter, replace=True)
    for metadata in MARKET_CATALOG:
        if metadata.market_id in {adapter.metadata.market_id for adapter in implemented_adapters}:
            continue
        blocker = VERIFIED_BLOCKERS.get(metadata.market_id)
        if blocker:
            registry.register_factory(
                metadata,
                lambda config=None, metadata=metadata, blocker=blocker: create_verified_blocked_adapter(
                    metadata,
                    config,
                    reason=str(blocker["reason"]),
                    references=blocker.get("references", ()),
                    last_reviewed=str(blocker.get("last_reviewed") or "2026-05-26"),
                ),
                replace=True,
            )
            continue
        registry.register_factory(
            metadata,
            lambda config=None, metadata=metadata: create_stub_adapter(metadata, config),
            replace=True,
        )
    return registry
