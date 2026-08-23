"""Additional platform metadata for the expanded prediction-market inventory.

The catalog is intentionally explicit: a platform can be visible in the GUI and
CLI without being presented as operationally supported.  Entries without a
verified adapter are paired with a blocker record in ``EXPANDED_VERIFIED_BLOCKERS``.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from .types import MarketCapabilities, MarketMetadata


XMARKET_CAPABILITIES = MarketCapabilities(
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
    kyc_required=False,
    region_limited=False,
)


PROBABLE_CAPABILITIES = MarketCapabilities(
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
    kyc_required=False,
    region_limited=True,
)


FANDUEL_PREDICTS_CAPABILITIES = MarketCapabilities(
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


METADAO_CAPABILITIES = MarketCapabilities(
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


SEER_CAPABILITIES = MarketCapabilities(
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


TRUEO_CAPABILITIES = MarketCapabilities(
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


ZEITGEIST_SDK_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    # Same guarded HybridRouter extrinsic boundary as the primary adapter.
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)


GNOSIS_PREDICTION_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    trade_history=True,
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


ZEITGEIST_POOL_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=False,
    alerts=True,
    paper_trading=True,
    # Pool-scoped live forwarding still requires reviewed pool metadata and an
    # externally signed HybridRouter extrinsic; the adapter never signs or settles.
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)


REALITY_ETH_CAPABILITIES = MarketCapabilities(
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


MATCHBOOK_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    # Authenticated matched bets contain the side, odds, stake, and identity
    # needed for a local copy preview.  The preview path never submits an
    # offer automatically.
    copy_trading=True,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)


SCICAST_CAPABILITIES = MarketCapabilities(
    # SciCast's documented Data Mart is a credentialed historical read API.
    # It supports question discovery, forecast snapshots, trade history,
    # alerts, and local paper previews, but it is not a live venue.
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
    credentials_required=True,
    kyc_required=False,
    region_limited=False,
)


PROPHET_EXCHANGE_CAPABILITIES = MarketCapabilities(
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


DFLOW_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=True,
    copy_trading=False,
    api_required=True,
    credentials_required=True,
    kyc_required=True,
    region_limited=True,
)


SPACE_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    # Price history, discovery, and books remain public, but Polymarket's
    # documented CLOB trade feed requires explicit readonly/L2 headers.
    credentials_required=True,
    kyc_required=False,
    region_limited=True,
)


BLINQ_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    # Blinq's documented product surface points at Polymarket markets.  The
    # underlying Polymarket public price-history feed and authenticated
    # read-only CLOB trade feed are safe to expose through this alias; these
    # reads do not imply Blinq wallet or leverage access.
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    # Blinq does not publish a leverage or wallet-execution API.  The adapter
    # is deliberately limited to the underlying public Polymarket surface.
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=False,
    region_limited=True,
)


COINBASE_PREDICTION_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    # Coinbase documents Kalshi as the venue behind its prediction-market
    # flow, so the public Kalshi trade and candlestick feeds are available on
    # this read-only alias as well.
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

ROBINHOOD_PREDICTION_CAPABILITIES = MarketCapabilities(
    market_discovery=True,
    event_listing=True,
    price_reading=True,
    orderbook_reading=True,
    trade_history=True,
    candle_history=True,
    alerts=True,
    paper_trading=True,
    live_trading=False,
    copy_trading=False,
    api_required=True,
    credentials_required=False,
    kyc_required=True,
    region_limited=True,
)

KALSHI_VIA_ROBINHOOD_CAPABILITIES = ROBINHOOD_PREDICTION_CAPABILITIES


PRDT_FINANCE_CAPABILITIES = MarketCapabilities(
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
    region_limited=True,
)


LAMAS_FINANCE_CAPABILITIES = MarketCapabilities(
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


ZETARIUM_CAPABILITIES = MarketCapabilities(
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


EXPANDED_MARKET_CATALOG: Tuple[MarketMetadata, ...] = (
    MarketMetadata(
        market_id="coinbase_prediction_markets",
        display_name="Coinbase Prediction Markets",
        homepage_url="https://help.coinbase.com/en/coinbase/trading-and-funding/prediction-markets/intro",
        description=(
            "Read-only Coinbase prediction-market alias over the official Kalshi venue market-data API. "
            "It supports discovery, contracts, prices, orderbooks, public trades, candlesticks, alerts, "
            "and local paper orders; "
            "Coinbase-specific live and copy-trading APIs are not published."
        ),
        capabilities=COINBASE_PREDICTION_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="probable",
        display_name="Probable",
        homepage_url="https://developer.probable.markets/",
        description=(
            "Official Probable market and CLOB API adapter for discovery, token prices, orderbooks, "
            "public wallet activity trade history, simulation-first wallet copy previews, point price history, "
            "alerts, paper orders, and guarded signed-order submission."
        ),
        capabilities=PROBABLE_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="kalshi_via_robinhood",
        display_name="Kalshi via Robinhood",
        homepage_url="https://robinhood.com/us/en/prediction-markets",
        description=(
            "Read-only Kalshi-through-Robinhood distribution alias over the official Kalshi market-data API. "
            "It supports discovery, contracts, prices, orderbooks, history, alerts, and local paper orders; "
            "Robinhood account execution and copy trading are not automated."
        ),
        capabilities=KALSHI_VIA_ROBINHOOD_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="fanduel_predicts",
        display_name="FanDuel Predicts",
        homepage_url="https://www.fanduel.com/predicts",
        description=(
            "Read-only FanDuel Predicts/OG alias using the official Crypto.com Predictions API for "
            "OG/CDNA event discovery, contracts, prices, alerts, and local dry-run orders. CME-listed "
            "contracts remain available through cme_prediction_markets; FanDuel account execution and "
            "copy trading are not automated."
        ),
        capabilities=FANDUEL_PREDICTS_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="seer",
        display_name="Seer",
        homepage_url="https://seer-3.gitbook.io/seer-documentation/developers/interact-with-seer",
        description=(
            "Official Seer serverless API adapter for market discovery, outcome prices, alerts, "
            "local paper orders, and guarded externally signed transactions to an explicitly reviewed "
            "third-party DEX; CLOB depth, wallet signing, approvals, and settlement remain operator-owned."
        ),
        capabilities=SEER_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="dflow",
        display_name="DFlow",
        homepage_url="https://pond.dflow.net/introduction",
        description=(
            "Official DFlow Metadata/Trade API adapter for event and market discovery, outcome prices, "
            "orderbooks, public trade history, paper orders, and guarded wallet-signed Solana transaction submission."
        ),
        capabilities=DFLOW_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="space",
        display_name="Space",
        homepage_url="https://docs.into.space/en/api/rest",
        description=(
            "Official Space REST adapter for public market discovery, binary/multi-outcome contracts, "
            "prices, orderbooks, public trade history, OHLCV candles, alerts, and local paper orders; "
            "wallet-signed live execution and copy trading remain unsupported while the public API release "
            "is pending."
        ),
        capabilities=SPACE_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="xmarket",
        display_name="Xmarket",
        homepage_url="https://docs.xmarket.app/developers/quick-start",
        description="Official Xmarket API adapter for market discovery, outcome prices, orderbooks, paper orders, and guarded API-key order submission.",
        capabilities=XMARKET_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="trueo",
        display_name="Trueo",
        homepage_url="https://docs.trueo.com/trading",
        description=(
            "Official Trueo Base on-chain adapter for TruthMarketManager discovery, immutable market fields, "
            "Uniswap V3 outcome prices, alerts, paper orders, and guarded externally signed transactions; "
            "CLOB depth and copy trading remain unsupported."
        ),
        capabilities=TRUEO_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="prdt_finance",
        display_name="PRDT Finance",
        homepage_url="https://prdt.finance/en",
        description=(
            "Configured PRDT Prediction-contract adapter for on-chain event discovery, bull/bear pool-share "
            "prices, alerts, and local paper intents. Explicit deployed Prediction addresses are required; "
            "CLOB depth, live wallet execution, settlement, and copy trading remain unsupported."
        ),
        capabilities=PRDT_FINANCE_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="synstation",
        display_name="SynStation",
        homepage_url="https://synstation.ai",
        description="Verified blocked: SynStation's official whitepaper is conceptual and no stable market-data, deployment, or order API contract has been validated.",
    ),
    MarketMetadata(
        market_id="gnosis_prediction_markets",
        display_name="Gnosis Prediction Markets",
        homepage_url="https://omen.eth.limo",
        description=(
            "Official Gnosis/Omen FixedProductMarketMaker adapter for market discovery, outcome prices, "
            "public FPMM trades, bounded derived candles, alerts, local paper orders, and guarded externally signed FPMM transactions; CLOB depth, "
            "collateral approval, settlement, and copy trading remain unsupported."
        ),
        capabilities=GNOSIS_PREDICTION_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="zeitgeist_sdk_markets",
        display_name="Zeitgeist SDK / Markets",
        homepage_url="https://docs.zeitgeist.pm/docs/build/sdk/v2/fetch-markets",
        description=(
            "Official Zeitgeist SDK/Markets alias using the documented Subsquid/indexer GraphQL market and asset "
            "contract for discovery, outcome prices, alerts, and paper orders."
        ),
        capabilities=ZEITGEIST_SDK_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="metadao",
        display_name="MetaDAO",
        homepage_url="https://docs.metadao.fi/protocol/analytics",
        description=(
            "Official MetaDAO Futarchy DEX API adapter for public DAO ticker discovery, bid/ask/price reads, "
            "alerts, local paper orders, and guarded externally signed Solana router transactions; orderbook "
            "depth, wallet signing, approvals, settlement, and copy trading remain operator-owned or unsupported."
        ),
        capabilities=METADAO_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="levr_bet",
        display_name="Levr Bet",
        homepage_url="https://levr.bet",
        description="Verified blocked: no stable official API, contract schema, and settlement fixtures have been validated for Levr Bet.",
    ),
    MarketMetadata(
        market_id="dexsport",
        display_name="Dexsport",
        homepage_url="https://dexsport.io/docs-home/",
        description="Verified blocked: Dexsport documents a betting protocol, but this app lacks a validated prediction-market data, wallet, and settlement adapter.",
    ),
    MarketMetadata(
        market_id="lamas_finance",
        display_name="Lamas Finance",
        homepage_url="https://docs.lamas.co/1.0",
        description=(
            "Official Lamas Finance Solana Anchor adapter for PricePredict and UpOrDown round discovery, "
            "pooled prices, alerts, local paper intents, and guarded externally signed predict transactions. "
            "CLOB depth, settlement, and copy trading remain unsupported."
        ),
        capabilities=LAMAS_FINANCE_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="zetarium_world",
        display_name="Zetarium World",
        homepage_url="https://docs.zetarium.world/docs",
        description=(
            "Reviewed BSC PredictionMarket adapter for Zetarium World V2: on-chain event discovery, "
            "pari-mutuel outcome prices, alerts, local paper intents, and guarded externally signed BUY "
            "transactions are implemented. CLOB depth, wallet signing, settlement, and copy trading remain unsupported."
        ),
        capabilities=ZETARIUM_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="blinq",
        display_name="Blinq",
        homepage_url="https://blinq.fi",
        description=(
            "Read-only Blinq alias over the official Polymarket market-data APIs for markets surfaced "
            "by Blinq. Discovery, prices, orderbooks, public trades, price-history points, alerts, and "
            "local paper orders are supported; "
            "Blinq leverage, deposits, live wallet execution, and copy trading remain unsupported."
        ),
        capabilities=BLINQ_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="zeitgeist_prediction_pools",
        display_name="Zeitgeist Prediction Pools",
        homepage_url="https://docs.zeitgeist.pm/docs/build/sdk/v2/fetch-markets",
        description=(
            "Official Zeitgeist pool-aware indexer adapter for market discovery, pool-backed outcome prices, "
            "alerts, and local paper orders; CLOB depth, wallet execution, and pool settlement remain unsupported."
        ),
        capabilities=ZEITGEIST_POOL_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="reality_eth_markets",
        display_name="Reality.eth Markets",
        homepage_url="https://reality.eth.limo",
        description=(
            "Official Reality.eth subgraph adapter for read-only question discovery, response-option listing, "
            "and lifecycle status; prices, price-triggered alerts, orderbooks, and trading are not part of the oracle protocol."
        ),
        capabilities=REALITY_ETH_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="sportstrade",
        display_name="SportsTrade",
        homepage_url="https://sportstrade.com",
        description="Verified blocked: Sporttrade officially ceased all wagering on 2026-05-25, so no production market or order integration is available.",
    ),
    MarketMetadata(
        market_id="prophet_exchange",
        display_name="Prophet Exchange",
        homepage_url="https://docs.prophetx.co/docs/integration",
        description=(
            "Official ProphetX Market Data and Trading API adapter for tournament/event/market discovery, "
            "available-quantity quotes, local paper orders, and guarded authenticated market-maker orders."
        ),
        capabilities=PROPHET_EXCHANGE_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="sporttrade_products",
        display_name="Sporttrade Prediction / Exchange Products",
        homepage_url="https://sporttrade.com",
        description="Verified blocked: Sporttrade officially ceased all wagering on 2026-05-25, so its prediction/exchange products are not an active production integration target.",
    ),
    MarketMetadata(
        market_id="matchbook",
        display_name="Matchbook",
        homepage_url="https://developers.matchbook.com/",
        description=(
            "Official Matchbook exchange API adapter for event/market discovery, decimal-odds prices, "
            "orderbooks, matched-bet history, simulation-first copy previews, paper orders, and guarded "
            "session-authenticated offers."
        ),
        capabilities=MATCHBOOK_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="scicast",
        display_name="SciCast",
        homepage_url="https://scicast.wordpress.com/wp-content/uploads/2014/10/scicast_datamart_guide_v1-21.pdf",
        description=(
            "Archive-only SciCast Data Mart adapter for documented question discovery, historical forecast and "
            "trade snapshots, alerts, and local paper previews; the retired service has no supported live venue."
        ),
        capabilities=SCICAST_CAPABILITIES,
    ),
    MarketMetadata(
        market_id="meta_arena",
        display_name="Meta Arena",
        homepage_url="https://docs.metaarena.world/",
        description="Verified blocked: Meta Arena is a game platform and does not expose a validated prediction-market API for this adapter model.",
    ),
)


def _blocker(
    reason: str,
    *references: str,
    last_reviewed: str = "2026-08-16",
) -> Dict[str, Any]:
    return {
        "reason": f"Verified {last_reviewed}: {reason}",
        "references": list(references),
        "last_reviewed": last_reviewed,
    }


EXPANDED_VERIFIED_BLOCKERS: Dict[str, Dict[str, Any]] = {
    "synstation": _blocker(
        "SynStation's official whitepaper describes a conceptual CLMM prediction protocol, but no stable market-data endpoint, deployment inventory, or order contract has been validated.",
        "https://synstation.ai",
        "https://synstation.notion.site/SynStation-Whitepaper-12dd359ec74180789fd2cef45609fa93",
        last_reviewed="2026-08-23",
    ),
    "levr_bet": _blocker(
        "No stable official API, contract schema, and settlement fixtures have been validated for Levr Bet.",
        "https://levr.bet",
        last_reviewed="2026-08-21",
    ),
    "dexsport": _blocker(
        "Dexsport documents prediction markets and smart-contract betting, but its official docs publish no stable market-data API, deployment inventory, or reviewed contract schema for this app; the current web interface cannot substitute for an integration contract.",
        "https://dexsport.io/docs-home/",
        "https://dexsport.io/prediction-markets/all/",
        last_reviewed="2026-08-21",
    ),
    "sportstrade": _blocker(
        "Sporttrade officially ceased all wagering on 2026-05-25; no active production market or order integration is available.",
        "https://getsporttrade.com/",
        "https://new.getsporttrade.com/",
    ),
    "sporttrade_products": _blocker(
        "Sporttrade officially ceased all wagering on 2026-05-25; its prediction/exchange products are not an active production integration target.",
        "https://getsporttrade.com/",
        "https://new.getsporttrade.com/",
    ),
    "meta_arena": _blocker(
        "Meta Arena is a game platform and does not expose a validated prediction-market API for this adapter model.",
        "https://docs.metaarena.world/",
    ),
}
