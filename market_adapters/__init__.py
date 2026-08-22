from __future__ import annotations

from .azuro import AzuroAdapter
from .base import MarketAdapter
from .betmgm import BetMGMAdapter
from .betfair import BetfairExchangeAdapter
from .blinq import BlinqAdapter
from .coinbase_prediction import CoinbasePredictionMarketsAdapter
from .catalog import MARKET_CATALOG, MARKET_IDS, get_market_metadata
from .crypto_com_predict import CryptoComPredictAdapter, FanaticsMarketsAdapter
from .draftkings_predictions import DraftKingsPredictionsAdapter
from .context_v2 import ContextV2Adapter
from .dflow import DFlowAdapter
from .drift_bet import DriftBetAdapter
from .frenzy import FrenzyFinanceAdapter
from .fanduel_predicts import FanDuelPredictsAdapter
from .errors import MarketAdapterError, MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .gemini import GeminiPredictionAdapter
from .good_judgment_open import GoodJudgmentOpenAdapter
from .hyperliquid import HyperliquidAdapter
from .hedgehog import HedgehogMarketsAdapter
from .ibkr_event_contracts import CMEPredictionMarketsAdapter, ForecastExAdapter, IBKREventContractsAdapter, IBKRForecastTraderAdapter
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
from .manifold import ManifoldAdapter
from .matchbook import MatchbookAdapter
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
from .registry import AdapterRegistry, VERIFIED_BLOCKERS, build_default_registry
from .runtime import AdapterRuntime, RateLimiter, ResolvedCredential, load_json_fixture, load_market_fixture
from .smarkets import SmarketsAdapter
from .seer import SeerAdapter
from .scicast import SciCastAdapter
from .space import SpaceAdapter
from .thales import ThalesMarketAdapter
from .trueo import TrueoAdapter
from .zetarium import ZetariumWorldAdapter
from .stub import StubMarketAdapter, VerifiedBlockedAdapter, create_stub_adapter, create_verified_blocked_adapter
from .sx_bet import SxBetAdapter
from .xo import XOMarketAdapter
from .xmarket import XMarketAdapter
from .types import (
    MarketCapabilities,
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
    MarketTrade,
)

__all__ = [
    "AdapterRegistry",
    "AdapterRuntime",
    "AugurAdapter",
    "AzuroAdapter",
    "BetMGMAdapter",
    "BetfairExchangeAdapter",
    "BlinqAdapter",
    "CoinbasePredictionMarketsAdapter",
    "CryptoComPredictAdapter",
    "DraftKingsPredictionsAdapter",
    "FanaticsMarketsAdapter",
    "ContextV2Adapter",
    "DFlowAdapter",
    "DriftBetAdapter",
    "FrenzyFinanceAdapter",
    "FanDuelPredictsAdapter",
    "GeminiPredictionAdapter",
    "GoodJudgmentOpenAdapter",
    "GnosisPredictionMarketsAdapter",
    "HyperliquidAdapter",
    "HedgehogMarketsAdapter",
    "IBKREventContractsAdapter",
    "IBKRForecastTraderAdapter",
    "IowaElectronicMarketsAdapter",
    "ForecastExAdapter",
    "CMEPredictionMarketsAdapter",
    "MARKET_CATALOG",
    "MARKET_IDS",
    "MarketAdapter",
    "MarketAdapterError",
    "MarketCapabilities",
    "MarketCandle",
    "MarketConfigurationError",
    "MarketContract",
    "MarketEvent",
    "MarketHTTPError",
    "MarketMetadata",
    "MarketTrade",
    "KalshiAdapter",
    "KalshiViaRobinhoodAdapter",
    "LamasFinanceAdapter",
    "LimitlessAdapter",
    "ManifoldAdapter",
    "MatchbookAdapter",
    "MetaculusAdapter",
    "MetaDAOAdapter",
    "MyriadAdapter",
    "NadexAdapter",
    "OpinionAdapter",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "OmenAdapter",
    "RealityEthMarketsAdapter",
    "PaperOrderRequest",
    "PaperOrderResult",
    "PolymarketAdapter",
    "ProbableAdapter",
    "PRDTFinanceAdapter",
    "ProphetExchangeAdapter",
    "PredictFunAdapter",
    "PredictItAdapter",
    "PriceSnapshot",
    "RateLimiter",
    "RobinhoodPredictionMarketsAdapter",
    "ResolvedCredential",
    "SxBetAdapter",
    "SmarketsAdapter",
    "SeerAdapter",
    "SciCastAdapter",
    "SpaceAdapter",
    "ThalesMarketAdapter",
    "TrueoAdapter",
    "ZetariumWorldAdapter",
    "StubMarketAdapter",
    "UnsupportedFeatureError",
    "VERIFIED_BLOCKERS",
    "VerifiedBlockedAdapter",
    "XOMarketAdapter",
    "XMarketAdapter",
    "ZeitgeistAdapter",
    "ZeitgeistPredictionPoolsAdapter",
    "ZeitgeistSdkMarketsAdapter",
    "build_default_registry",
    "create_stub_adapter",
    "create_verified_blocked_adapter",
    "get_market_metadata",
    "load_json_fixture",
    "load_market_fixture",
]

