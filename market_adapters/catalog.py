from __future__ import annotations

from typing import Dict, Tuple

from .expanded_catalog import EXPANDED_MARKET_CATALOG, ROBINHOOD_PREDICTION_CAPABILITIES
from .types import MarketCapabilities, MarketMetadata


POLYMARKET_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

KALSHI_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated portfolio fills contain outcome, bid/ask direction,
    # price, size, and fill identity for local simulation-first previews.
    # Copy previews never submit a live order automatically.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

MANIFOLD_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=False,
)

METACULUS_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    candle_history=True,
    alerts=True,
    paper_trading=False,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=False,
)

GOOD_JUDGMENT_OPEN_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

PREDICTIT_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

BETMGM_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

CRYPTO_COM_PREDICT_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=False,
)

DRAFTKINGS_PREDICTIONS_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

FANATICS_MARKETS_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

NADEX_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

LIMITLESS_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated portfolio history exposes complete fills for local,
    # simulation-first copy previews; previews never submit live orders.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

SX_BET_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated v3 fills expose complete backed-outcome executions for
    # local, simulation-first copy previews; previews never submit live bets.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

AZURO_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=True,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

AUGUR_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=False,
    orderbook_reading=False,
    alerts=False,
    paper_trading=False,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

OMEN_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Public FPMMTrade.creator rows support bounded, simulation-first wallet
    # activity previews; no live copy submission is performed.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

ZEITGEIST_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    # Guarded forwarding of an operator-reviewed, externally signed
    # HybridRouter Substrate extrinsic; signing and settlement remain external.
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

GEMINI_PREDICTION_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated filled order history contains event/instrument identity,
    # BUY/SELL direction, filled quantity, price, timestamp, and order id for
    # simulation-first previews; previews never submit live orders.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

MYRIAD_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The documented public /users/:address/events feed supports
    # simulation-first wallet activity copy. Live execution remains guarded
    # behind the signed Myriad order path.
    copy_trading=True,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

OPINION_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Live execution is guarded by the optional official Opinion CLOB SDK,
    # explicit live-order gates, and BNB-chain credentials. Copy intents remain
    # simulation-first and never submit orders automatically.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

PREDICT_FUN_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The authenticated /account/activity feed contains matched fills with
    # market, outcome, side, size, price, and timestamp. Copy remains
    # simulation-first and is restricted to the JWT-authenticated account.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)

XO_MARKET_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The authenticated /trades feed contains complete, normalized fills
    # suitable for simulation-first copy previews.  Copy never submits a
    # live order automatically.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

BETFAIR_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated current orders expose matched side, average odds, stake,
    # and bet identity for local simulation-first previews.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

HYPERLIQUID_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The documented userFills endpoint exposes public HIP-4 wallet activity;
    # copy remains simulation-first and live submission still needs a signed
    # HyperCore payload.
    copy_trading=True,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

IBKR_EVENT_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The documented authenticated account-trades feed has execution
    # identity, event conid, B/S direction, price, and filled size for local
    # simulation-first copy previews; no live order is submitted.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

CONTEXT_V2_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # The official SDK documents read-only orders.list filtering by trader and
    # filled status; the adapter turns complete rows into paper-only copy
    # previews and never submits a live order from activity polling.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

THALES_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

SMARKETS_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)

DRIFT_BET_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    # The public Drift BET data API is read-only.  Live execution still
    # requires a wallet-signed Drift/Solana transaction and collateral flow
    # that this adapter deliberately does not submit.
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

IEM_CAPABILITIES = MarketCapabilities(
    # IEM's documented surface is an explicit inventory of official
    # historical price files; it does not publish dynamic discovery or a
    # current quote/order API.
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=False,
)

HYPERMIND_CAPABILITIES = MarketCapabilities(
    # Hypermind's official report links a documented historical trade export
    # and winning-outcomes export.  They are read-only archive data, not a
    # public orderbook or execution API.
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=False,
)

FRENZY_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    # Frenzy live bets require an oracle-signed BetAck in addition to a
    # wallet signature; this adapter only emits a paper EIP-712 intent.
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

HEDGEHOG_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)

STUB_CAPABILITIES = MarketCapabilities()

MARKET_CATALOG: Tuple[MarketMetadata, ...] = (
    MarketMetadata(
        market_id="polymarket",
        display_name="Polymarket",
        default_enabled=True,
        homepage_url="https://polymarket.com",
        description="Existing Polymarket alert, wallet tracking, and guarded copy-trading support.",
        capabilities=POLYMARKET_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="kalshi",
        display_name="Kalshi",
        homepage_url="https://kalshi.com",
        description=(
            "Official Kalshi REST adapter with authenticated fill copy previews, market data, dry-run orders, "
            "and guarded live-order support."
        ),
        capabilities=KALSHI_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="predictit",
        display_name="PredictIt",
        homepage_url="https://www.predictit.org",
        description="Official public market-data API adapter for read-only political market data and dry-run orders.",
        capabilities=PREDICTIT_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="robinhood_prediction_markets",
        display_name="Robinhood Prediction Markets",
        homepage_url="https://robinhood.com",
        description=(
            "Read-only Robinhood Prediction Markets alias over the official Kalshi venue market-data API. "
            "It supports discovery, contracts, prices, orderbooks, history, alerts, and local paper orders; "
            "Robinhood account execution and copy trading are not automated."
        ),
        capabilities=ROBINHOOD_PREDICTION_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="fanatics_markets",
        display_name="Fanatics Markets",
        homepage_url="https://fanaticsmarkets.com",
        description=(
            "Read-only Fanatics Markets/CDNA alias using the official Crypto.com Predictions API for "
            "event discovery, contracts, prices, alerts, and local dry-run orders; Fanatics order APIs "
            "are not published and live/copy trading remain unsupported."
        ),
        capabilities=FANATICS_MARKETS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="draftkings_predictions",
        display_name="DraftKings Predictions",
        homepage_url="https://www.draftkings.com",
        description=(
            "Read-only DraftKings Predictions/CDNA alias using the official Crypto.com Predictions API for "
            "event discovery, contracts, prices, alerts, and local dry-run orders; CME-listed contracts "
            "remain available through cme_prediction_markets. DraftKings account execution and copy trading "
            "are not automated."
        ),
        capabilities=DRAFTKINGS_PREDICTIONS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="ibkr_forecasttrader",
        display_name="Interactive Brokers ForecastTrader / IBKR Prediction Markets",
        homepage_url="https://www.interactivebrokers.com",
        description=(
            "Official IBKR Client Portal Web API event-contract adapter for ForecastTrader/ForecastEx "
            "discovery, conids, top-of-book prices, execution copy previews, alerts, paper orders, and guarded live orders."
        ),
        capabilities=IBKR_EVENT_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="forecastex",
        display_name="ForecastEx",
        homepage_url="https://www.forecastex.com",
        description=(
            "Official ForecastEx event-contract adapter routed through the IBKR Client Portal Web API "
            "for discovery, conids, top-of-book prices, execution copy previews, alerts, paper orders, and guarded live orders."
        ),
        capabilities=IBKR_EVENT_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="cme_prediction_markets",
        display_name="CME Group Prediction Markets",
        homepage_url="https://www.cmegroup.com",
        description=(
            "Official CME event-contract adapter routed through the IBKR Client Portal Web API for "
            "product discovery, event conids, top-of-book prices, execution copy previews, alerts, paper orders, and guarded orders."
        ),
        capabilities=IBKR_EVENT_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="nadex",
        display_name="Nadex",
        homepage_url="https://www.nadex.com",
        description=(
            "Read-only Nadex/CDNA prediction-event alias backed by the official Crypto.com Predictions "
            "Market Data API for event discovery, contracts, prices, alerts, and local dry-run orders. "
            "Nadex account trading, DCM/FIX depth, knock-out products, and copy trading remain unsupported."
        ),
        capabilities=NADEX_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="crypto_com_predict",
        display_name="Crypto.com Predict / CDNA",
        homepage_url="https://crypto.com",
        description=(
            "Official Crypto.com Predictions Market Data API adapter for anonymous event discovery, "
            "contracts, prices, alerts, and dry-run orders."
        ),
        capabilities=CRYPTO_COM_PREDICT_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="hyperliquid",
        display_name="Hyperliquid",
        homepage_url="https://hyperliquid.xyz",
        description=(
            "Official Hyperliquid HIP-4 outcome-market adapter for outcome discovery, prices, "
            "orderbooks, paper orders, and guarded externally signed orders."
        ),
        capabilities=HYPERLIQUID_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="myriad_markets",
        display_name="Myriad Markets",
        homepage_url="https://myriad.markets",
        description=(
            "Official Myriad Protocol API adapter for grouped event/market discovery, outcome prices, orderbooks, "
            "public wallet-event activity, simulation-first copy intents, dry-run quote payloads, and guarded signed order submission."
        ),
        capabilities=MYRIAD_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="context_v2",
        display_name="Context V2",
        homepage_url="https://context.markets",
        description=(
            "Official Context Markets v2 API adapter for market discovery, outcome prices, orderbooks, "
            "market activity trades, binary price history, filled wallet-order activity, simulation-first copy "
            "previews, paper orders, and guarded wallet-signed order submission."
        ),
        capabilities=CONTEXT_V2_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="frenzy_finance",
        display_name="Frenzy Finance",
        homepage_url="https://frenzy.finance",
        description=(
            "Official Base contract adapter for configured price-range grid specs, settlement history, and "
            "dry-run BetIntent previews; live execution requires an oracle acknowledgement and wallet signing."
        ),
        capabilities=FRENZY_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="xo_market",
        display_name="XO Market",
        homepage_url="https://xotrade.co",
        description=(
            "Official XO Markets HMAC REST adapter for authenticated market data, orderbooks, public trades, "
            "OHLCV candles, authenticated account/settlement/audit recovery, dry-run orders, and guarded live "
            "order posting/cancellation."
        ),
        capabilities=XO_MARKET_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="manifold",
        display_name="Manifold Markets",
        homepage_url="https://manifold.markets",
        description=(
            "Official Manifold REST API adapter for market discovery, probabilities, public user-bet activity, "
            "simulation-first copy intents, dry-run orders, and guarded MANA betting."
        ),
        capabilities=MANIFOLD_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="metaculus",
        display_name="Metaculus",
        homepage_url="https://www.metaculus.com",
        description="Official Metaculus API adapter for authenticated read-only forecasting questions and probabilities.",
        capabilities=METACULUS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="good_judgment_open",
        display_name="Good Judgment Open",
        homepage_url="https://www.gjopen.com",
        description=(
            "Credentialed Cultivate Forecasts API adapter for Good Judgment Open questions, answer probabilities, "
            "irregular forecast history, local paper previews, and guarded forecast submission. The instance URL, "
            "account eligibility, and live submission approval remain operator-owned gates."
        ),
        capabilities=GOOD_JUDGMENT_OPEN_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="hypermind",
        display_name="Hypermind",
        homepage_url="https://www.hypermind.com",
        description=(
            "Archive-only adapter for Hypermind's official trade-level CSV and winning-outcomes export; "
            "orderbooks, live trading, and copy trading remain unsupported."
        ),
        capabilities=HYPERMIND_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="iowa_electronic_markets",
        display_name="Iowa Electronic Markets",
        homepage_url="https://iem.uiowa.edu",
        description=(
            "Official historical price-file adapter with explicit archive inventory, daily candles, latest "
            "archived prices, alerts, and dry-run orders; current quote/order APIs remain unsupported."
        ),
        capabilities=IEM_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="infer",
        display_name="INFER / INFER-pub",
        homepage_url="https://www.infer-pub.com",
        description="Verified blocked: INFER/RFI is now a permanent read-only archive with no supported API/export contract.",
    ),
    MarketMetadata(market_id="fact_machine", display_name="Fact Machine", homepage_url="https://factmachine.io"),
    MarketMetadata(
        market_id="opinion_labs",
        display_name="Opinion Labs",
        homepage_url="https://opinion.trade",
        description=(
            "Official Opinion OpenAPI adapter for authenticated market data, orderbooks, prices, "
            "filled wallet-trade activity, and simulation-first copy intents; guarded live limit/market "
            "orders use the optional official CLOB SDK and remain disabled by default."
        ),
        capabilities=OPINION_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="gemini_titan",
        display_name="Gemini Titan / Gemini Predictions",
        homepage_url="https://www.gemini.com",
        description="Official Gemini Prediction Markets API adapter for public event discovery, contracts, orderbooks, prices, dry-run orders, and guarded authenticated limit orders.",
        capabilities=GEMINI_PREDICTION_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="augur",
        display_name="Augur",
        homepage_url="https://augur.net",
        description="Legacy Augur v2 read-only market/outcome adapter using the documented subgraph schema with a user-configured GraphQL endpoint.",
        capabilities=AUGUR_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="betmgm",
        display_name="BetMGM",
        homepage_url="https://www.betmgm.com",
        description=(
            "Official BetMGM partner Sports API adapter for fixture, market, option, and implied-price reads, "
            "alerts, and local paper orders; the partner API publishes no supported order or account-activity endpoint."
        ),
        capabilities=BETMGM_CAPABILITIES,
    ),
    MarketMetadata(market_id="prizepicks", display_name="PrizePicks", homepage_url="https://www.prizepicks.com"),
    MarketMetadata(market_id="underdog_sports", display_name="Underdog Sports", homepage_url="https://underdogfantasy.com"),
    MarketMetadata(
        market_id="drift_bet",
        display_name="Drift BET",
        homepage_url="https://www.drift.trade",
        description=(
            "Official Drift Data API adapter for explicitly configured BET market symbols, prediction prices, "
            "public fills, bounded derived candles, alerts, and dry-run orders; binary orderbooks, wallet-signed "
            "live orders, and copy trading remain disabled."
        ),
        capabilities=DRIFT_BET_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="thales_market",
        display_name="Thales Market",
        homepage_url="https://thalesmarket.io",
        description=(
            "Official Thales Markets REST adapter for public AMM market discovery, positional prices, "
            "quote-backed paper orders, and alerts; live wallet transactions remain explicitly disabled."
        ),
        capabilities=THALES_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="hedgehog_markets",
        display_name="Hedgehog Markets",
        homepage_url="https://hedgehog.markets",
        description=(
            "Official Hedgehog HPL Parimutuel/Eclipse adapter for on-chain MarketV1 discovery, pooled outcome "
            "prices, alerts, and dry-run DepositV1 intents; CLOB depth, wallet-signed live execution, and copy "
            "trading remain disabled."
        ),
        capabilities=HEDGEHOG_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="omen",
        display_name="Omen",
        homepage_url="https://omen.eth.limo",
        description="Legacy Omen/Gnosis FixedProductMarketMaker subgraph adapter for AMM markets, marginal prices, public FPMM trades, bounded derived candles, creator-scoped simulation-first copy previews, alerts, paper orders, and guarded externally signed live transactions.",
        capabilities=OMEN_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="zeitgeist",
        display_name="Zeitgeist",
        homepage_url="https://zeitgeist.pm",
        description="Official Zeitgeist Subsquid/indexer adapter for market discovery, outcome asset prices, alerts, and paper orders.",
        capabilities=ZEITGEIST_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="azuro",
        display_name="Azuro",
        homepage_url="https://azuro.org",
        description="Official Azuro V3 backend/feed API adapter for games, conditions, odds, bettor-scoped single-bet activity, simulation-first copy previews, WebSocket subscriptions, dry-run bets, and guarded pre-signed live order posting.",
        capabilities=AZURO_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="sx_bet",
        display_name="SX Bet / SX Network",
        homepage_url="https://sx.bet",
        description="Official SX Bet REST/WebSocket adapter for sports market data, orderbooks, dry-run orders, and guarded signed live orders.",
        capabilities=SX_BET_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="limitless_exchange",
        display_name="Limitless Exchange",
        homepage_url="https://limitless.exchange",
        description=(
            "Official Limitless REST adapter for market data, orderbooks, authenticated portfolio-history "
            "copy previews, dry-run orders, and guarded HMAC live orders."
        ),
        capabilities=LIMITLESS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="predict_fun",
        display_name="Predict.fun",
        homepage_url="https://predict.fun",
        description="Official Predict.fun REST API adapter for market discovery, orderbooks, prices, public match history, timeseries, authenticated account activity, simulation-first account copy intents, dry-run orders, and guarded signed/relay order operations.",
        capabilities=PREDICT_FUN_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="smarkets",
        display_name="Smarkets",
        homepage_url="https://smarkets.com",
        description=(
            "Official Smarkets v3 REST adapter for event/market/contract discovery, quote orderbooks, "
            "paper orders, authenticated order/account reads, and guarded session-authenticated order "
            "submission/cancellation."
        ),
        capabilities=SMARKETS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="betfair_exchange",
        display_name="Betfair Exchange",
        homepage_url="https://www.betfair.com/exchange",
        description=(
            "Official Betfair Exchange API adapter for authenticated market discovery, best-offer orderbooks, "
            "matched-order copy previews, prices, dry-run orders, and guarded placeOrders support."
        ),
        capabilities=BETFAIR_CAPABILITIES,
    ),
    MarketMetadata(market_id="probo", display_name="Probo", homepage_url="https://probo.in"),
) + EXPANDED_MARKET_CATALOG

MARKET_IDS = tuple(m.market_id for m in MARKET_CATALOG)
_MARKET_BY_ID: Dict[str, MarketMetadata] = {m.market_id: m for m in MARKET_CATALOG}


def get_market_metadata(market_id: str) -> MarketMetadata:
    normalized = str(market_id or "").strip().lower()
    return _MARKET_BY_ID[normalized]
