from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError
from .identity import require_activity_identity
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
    MarketTrade,
)


DEFAULT_DFLOW_METADATA_BASE_URL = "https://dev-prediction-markets-api.dflow.net"
DEFAULT_DFLOW_TRADE_BASE_URL = "https://dev-quote-api.dflow.net"
DEFAULT_DFLOW_SETTLEMENT_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DFLOW_REFERENCES = (
    "https://pond.dflow.net/introduction",
    "https://dflow.mintlify.app/build/metadata-api/markets/market-by-mint",
    "https://dflow.mintlify.app/build/metadata-api/orderbook/orderbook-by-mint",
    "https://dflow.mintlify.app/build/metadata-api/trades/trades-by-mint",
    "https://dflow.mintlify.app/build/metadata-api/trades/onchain-trades",
    "https://dflow.mintlify.app/build/recipes/prediction-markets/decrease-position",
)


class DFlowAdapter(MarketAdapter):
    """DFlow Metadata/Trade API adapter with a wallet-signed live-order boundary.

    DFlow builds a Solana transaction which must be signed by the user's wallet
    and submitted to a Solana RPC endpoint.  This adapter deliberately accepts a
    signed transaction in order metadata instead of handling private keys.
    """

    metadata = get_market_metadata("dflow")
    live_order_sides = ("BUY", "SELL")
    account_recovery_operations = ("account_activity",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._event_cache: Dict[str, Mapping[str, Any]] = {}
        self._market_cache: Dict[str, Mapping[str, Any]] = {}

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        credential = self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), label="DFLOW_API_KEY")
        wallet = self.resolve_credential(
            "dflow_wallet_address",
            ("DFLOW_WALLET_ADDRESS", "DFLOW_USER_PUBLIC_KEY"),
            label="DFLOW_WALLET_ADDRESS",
        )
        rpc = self.solana_rpc_url
        health.update(
            {
                "metadata_api_base_url": self.metadata_api_base_url,
                "trade_api_base_url": self.trade_api_base_url,
                "solana_rpc_configured": bool(rpc),
                "settlement_mint": self.settlement_mint,
                "references": list(DFLOW_REFERENCES),
                "credential_sources": [
                    item
                    for item in (
                        {"name": credential.name, "source": credential.source} if credential else None,
                        {"name": wallet.name, "source": wallet.source} if wallet else None,
                    )
                    if item
                ],
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "wallet_signed_transaction_required": True,
            }
        )
        return health

    @property
    def metadata_api_base_url(self) -> str:
        value = self.config.get("dflow_metadata_api_base_url") or self.config.get("api_base_url")
        return str(value or DEFAULT_DFLOW_METADATA_BASE_URL).rstrip("/")

    @property
    def trade_api_base_url(self) -> str:
        value = self.config.get("dflow_trade_api_base_url") or self.config.get("trade_api_base_url")
        return str(value or DEFAULT_DFLOW_TRADE_BASE_URL).rstrip("/")

    @property
    def settlement_mint(self) -> str:
        value = self.config.get("dflow_settlement_mint") or os.getenv("DFLOW_SETTLEMENT_MINT")
        return str(value or DEFAULT_DFLOW_SETTLEMENT_MINT).strip()

    @property
    def solana_rpc_url(self) -> str:
        value = self.config.get("dflow_solana_rpc_url") or self.config.get("solana_rpc_url")
        value = value or os.getenv("DFLOW_SOLANA_RPC_URL") or os.getenv("SOLANA_RPC_URL")
        return str(value or "").strip()

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 200))
        params: Dict[str, Any] = {
            "limit": desired,
            "status": str(self.config.get("dflow_event_status") or "active"),
            "withNestedMarkets": "true",
        }
        series = self.config.get("dflow_series_tickers") or self.config.get("dflow_series_ticker")
        if series:
            params["seriesTickers"] = str(series)
        payload = self._metadata_get("/api/v1/events", params=params)
        events = self._rows(payload, "events", "data")
        needle = str(query or "").strip().lower()
        if needle:
            events = [event for event in events if needle in self._search_text(event)]
        result: List[MarketEvent] = []
        for event in events[:desired]:
            event_id = self._event_id(event)
            if event_id:
                self._event_cache[event_id] = event
            result.append(self._event_from_payload(event))
        return result

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        clean_event_id = str(event_id or "").strip()
        if not clean_event_id:
            raise MarketConfigurationError("DFlow event id cannot be empty.")
        event = self._event_cache.get(clean_event_id)
        if event is None:
            payload = self._metadata_get(
                "/api/v1/events",
                params={
                    "eventTicker": clean_event_id,
                    "limit": 100,
                    "status": str(self.config.get("dflow_event_status") or "active"),
                    "withNestedMarkets": "true",
                },
            )
            rows = self._rows(payload, "events", "data")
            event = next((row for row in rows if self._event_id(row) == clean_event_id), rows[0] if rows else None)
            if event is not None:
                self._event_cache[clean_event_id] = event
        markets = self._market_rows(event or {})
        if not markets:
            payload = self._metadata_get(
                "/api/v1/markets",
                params={"eventTicker": clean_event_id, "limit": 100},
            )
            markets = self._rows(payload, "markets", "data")
        contracts: List[MarketContract] = []
        for market in markets:
            self._cache_market(market)
            contracts.extend(self._contracts_from_market(market, event_id=clean_event_id))
        return contracts

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        market, mint, side, canonical = self._resolve_contract(contract_id)
        payload = self._metadata_get(f"/api/v1/orderbook/by-mint/{quote(mint, safe='')}")
        book = self._mapping_payload(payload)
        bids_raw = book.get("yes_bids") if side == "YES" else book.get("no_bids")
        asks_raw = book.get("no_asks") if side == "YES" else book.get("yes_asks")
        if asks_raw is None:
            opposite_bids = book.get("no_bids") if side == "YES" else book.get("yes_bids")
            asks = self._complement_levels(opposite_bids)
        else:
            asks = self._levels(asks_raw)
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            bids=self._levels(bids_raw, descending=True),
            asks=asks,
            raw={"market": dict(market), "orderbook": dict(book), "side": side},
        )

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market, _mint, side, canonical = self._resolve_contract(contract_id)
        book = self.get_orderbook(canonical)
        bid = book.bids[0].price if book.bids else None
        ask = book.asks[0].price if book.asks else None
        prefix = "yes" if side == "YES" else "no"
        if bid is None:
            bid = self._safe_probability(market.get(f"{prefix}Bid"))
        if ask is None:
            ask = self._safe_probability(market.get(f"{prefix}Ask"))
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=midpoint if midpoint is not None else (bid if bid is not None else ask),
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="dflow_metadata",
            raw=dict(book.raw),
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return public DFlow trades for an outcome mint.

        DFlow's Metadata API exposes the trade feed by ledger/outcome mint.
        The feed requires an API key and uses Unix-second ``minTs``/``maxTs``
        filters; the normalized adapter surface intentionally does not expose
        DFlow's cursor, so callers receive the bounded page returned here.
        """

        self.ensure_capability("trade_history")
        self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), required=True, label="DFLOW_API_KEY")
        market, mint, outcome, canonical = self._resolve_contract(contract_id)
        params: Dict[str, Any] = {"limit": self._history_limit(limit)}
        if after is not None:
            params["minTs"] = self._history_timestamp(after, "after")
        if before is not None:
            params["maxTs"] = self._history_timestamp(before, "before")

        payload = self._metadata_get(f"/api/v1/trades/by-mint/{quote(mint, safe='')}", params=params)
        ticker = self._market_id(market).upper()
        trades: List[MarketTrade] = []
        for raw in self._rows(payload, "trades", "data"):
            row_ticker = str(raw.get("ticker") or "").strip().upper()
            if row_ticker and row_ticker != ticker:
                continue
            price = self._trade_price(raw, outcome)
            size = self._positive_number(raw.get("countFp") or raw.get("count") or raw.get("quantity"))
            trade_id = str(raw.get("tradeId") or raw.get("trade_id") or raw.get("id") or "").strip()
            if price is None or size is None or not trade_id:
                continue
            taker_side = str(raw.get("takerSide") or raw.get("taker_side") or "").strip().upper()
            side = taker_side if taker_side in {"BUY", "SELL", "YES", "NO"} else outcome
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=self._timestamp_seconds(raw.get("createdTime") or raw.get("created_time") or raw.get("timestamp")),
                    raw=dict(raw),
                )
            )
        return trades

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return bounded wallet-filtered DFlow on-chain fills.

        DFlow's documented ``onchain-trades`` endpoint is the only official
        wallet-scoped fill feed.  It includes the input/output mints, outcome
        ticker, contract count, probability price, wallet, and transaction
        signature needed for a simulation-first copy preview.  Rows that
        cannot be mapped to a configured YES/NO mint are skipped rather than
        guessed.
        """

        self.ensure_capability("copy_trading")
        identity = require_activity_identity(self.market_id, wallet_address)
        wallet = identity.split(":", 1)[1] if identity.lower().startswith("solana:") else identity
        desired = self._activity_limit(limit)
        self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), required=True, label="DFLOW_API_KEY")
        payload = self._metadata_get(
            "/api/v1/onchain-trades",
            params={"wallet": wallet, "limit": desired, "sortBy": "createdAt", "sortOrder": "desc"},
        )
        activities: List[Dict[str, Any]] = []
        for raw in self._rows(payload, "trades", "data"):
            activity = self._normalize_onchain_activity(identity, raw)
            if activity is not None:
                activities.append(activity)
            if len(activities) >= desired:
                break
        return activities

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read DFlow's documented wallet-filtered on-chain fill feed."""

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "DFlow account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        wallet = kwargs.get("wallet") or kwargs.get("address") or kwargs.get("trader")
        identity = require_activity_identity(self.market_id, wallet)
        desired = self._activity_limit(kwargs.get("limit", 25))
        self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), required=True, label="DFLOW_API_KEY")
        raw_wallet = identity.split(":", 1)[1] if identity.lower().startswith("solana:") else identity
        params: Dict[str, Any] = {
            "wallet": raw_wallet,
            "limit": desired,
            "sortBy": "createdAt",
            "sortOrder": "desc",
        }
        ticker = str(kwargs.get("ticker") or kwargs.get("market_id") or "").strip()
        if ticker:
            params["ticker"] = ticker
        mint = str(kwargs.get("mint") or kwargs.get("token_id") or "").strip()
        if mint:
            params["mint"] = mint
        cursor = self._activity_cursor(kwargs.get("cursor"))
        if cursor:
            params["cursor"] = cursor
        payload = self._metadata_get("/api/v1/onchain-trades", params=params)
        activities = [
            activity
            for raw in self._rows(payload, "trades", "data")
            if (activity := self._normalize_onchain_activity(identity, raw)) is not None
        ]
        return {
            "source": "dflow_onchain_trades",
            "endpoint": "/api/v1/onchain-trades",
            "wallet": identity,
            "limit": desired,
            "ticker": ticker or None,
            "mint": mint or None,
            "coverage": "bounded_wallet_filtered",
            "activity": activities[:desired],
            "raw": payload,
        }

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive bounded OHLCV candles from DFlow's documented trade feed."""

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = self._history_timestamp(from_timestamp, "from_timestamp") if from_timestamp is not None else None
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError("DFlow candle history requires to_timestamp to be at or after from_timestamp.")

        trades = self.list_trades(
            contract_id,
            limit=self._candle_trade_limit(),
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in trades:
            if trade.timestamp is None or trade.timestamp < 0:
                continue
            if start_ts is not None and trade.timestamp < start_ts:
                continue
            if end_ts is not None and trade.timestamp > end_ts:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
            bucket = buckets.setdefault(
                bucket_timestamp,
                {"open": trade.price, "high": trade.price, "low": trade.price, "close": trade.price, "volume": 0.0, "trade_ids": []},
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            bucket["volume"] += max(0.0, float(trade.size))
            bucket["trade_ids"].append(trade.trade_id)

        _market, _mint, _outcome, canonical = self._resolve_contract(contract_id)
        return [
            MarketCandle(
                market_id=self.market_id,
                contract_id=canonical,
                timestamp=float(bucket_timestamp),
                open=float(bucket["open"]),
                high=float(bucket["high"]),
                low=float(bucket["low"]),
                close=float(bucket["close"]),
                volume=float(bucket["volume"]),
                raw={
                    "source": "dflow_public_trade_feed",
                    "derived": True,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self._validate_order(order)
        _market, mint, side, canonical = self._resolve_contract(order.contract_id)
        request = self._trade_request(order, mint)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=canonical,
            accepted=True,
            message=(
                f"DRY RUN: would request DFlow {str(order.side).upper()} {side} order for {float(order.size):g} contracts"
                + (f" at limit {float(order.limit_price):.4f}" if order.limit_price is not None else "")
            ),
            average_price=self._safe_probability(order.limit_price),
            raw={"trade_request": request, "outcome_side": side},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self._validate_order(order)
        preflight = self.preflight_live_order(order)
        _market, mint, side, canonical = self._resolve_contract(order.contract_id)
        signed_transaction = str(
            order.metadata.get("signed_transaction") or order.metadata.get("signedTransaction") or ""
        ).strip()
        if not signed_transaction:
            raise MarketConfigurationError(
                "DFlow live orders require order.metadata['signed_transaction'] containing a wallet-signed base64 transaction."
            )
        if not self.solana_rpc_url:
            raise MarketConfigurationError(
                "DFlow live orders require dflow_solana_rpc_url or DFLOW_SOLANA_RPC_URL for transaction submission."
            )
        self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), required=True, label="DFLOW_API_KEY")
        rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [signed_transaction, {"encoding": "base64", "skipPreflight": False}],
        }
        response = self.runtime.request_json("POST", self.solana_rpc_url, json_body=rpc_payload)
        if isinstance(response, Mapping) and response.get("error"):
            raise MarketConfigurationError("DFlow Solana RPC rejected the signed transaction.")
        return {
            "market_id": self.market_id,
            "contract_id": canonical,
            "outcome_side": side,
            "live": True,
            "preflight": preflight,
            "submission": "solana_rpc_sendTransaction",
            "response": response,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("DFlow activity has no outcome-mint contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("DFlow activity side must be BUY or SELL.")
        size = self._positive_number(activity.get("size") or activity.get("contracts"))
        if size is None:
            raise MarketConfigurationError("DFlow activity contracts must be positive and numeric.")
        raw_price = activity.get("price")
        limit_price = self._safe_probability(raw_price)
        if limit_price is None:
            raise MarketConfigurationError("DFlow activity price must be between 0 and 1.")
        # Resolve the canonical mint before constructing the paper order so a
        # copied row cannot redirect execution to an unlisted outcome.
        _market, _mint, _outcome, canonical = self._resolve_contract(contract_id)
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=canonical,
                side=side,
                size=size,
                limit_price=limit_price,
                metadata={"activity": dict(activity), "source": "dflow_onchain_trades"},
            )
        )

    def _normalize_onchain_activity(
        self, identity: str, raw: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        ticker = str(raw.get("marketTicker") or raw.get("market_ticker") or raw.get("ticker") or "").strip()
        if not ticker:
            return None
        input_mint = str(raw.get("inputMint") or raw.get("input_mint") or "").strip()
        output_mint = str(raw.get("outputMint") or raw.get("output_mint") or "").strip()
        if not input_mint or not output_mint:
            return None
        market = self._market_cache.get(ticker)
        if market is None:
            try:
                payload = self._metadata_get("/api/v1/markets", params={"ticker": ticker, "limit": 1})
            except Exception:
                return None
            rows = self._rows(payload, "markets", "data")
            market = rows[0] if rows else None
            if market is not None:
                self._cache_market(market)
        if not market:
            return None
        account, settlement = self._account_for_market(market)
        yes_mint = self._mint(account, "yesMint", market.get("yesMint"))
        no_mint = self._mint(account, "noMint", market.get("noMint"))
        outcome_mint = ""
        order_side = ""
        if input_mint == settlement and output_mint in {yes_mint, no_mint}:
            outcome_mint, order_side = output_mint, "BUY"
        elif output_mint == settlement and input_mint in {yes_mint, no_mint}:
            outcome_mint, order_side = input_mint, "SELL"
        if not outcome_mint:
            return None
        outcome = "YES" if outcome_mint == yes_mint else "NO"
        size = self._positive_number(raw.get("contracts") or raw.get("count") or raw.get("quantity"))
        price = self._safe_probability(
            raw.get("usdPricePerContract")
            or raw.get("usd_price_per_contract")
            or raw.get("price")
            or raw.get("priceDollars")
        )
        timestamp = self._timestamp_seconds(raw.get("createdAt") or raw.get("created_at") or raw.get("timestamp"))
        transaction = str(
            raw.get("transactionSignature") or raw.get("transaction_signature") or raw.get("signature") or ""
        ).strip()
        activity_id = str(raw.get("id") or transaction).strip()
        if size is None or price is None or timestamp is None or not activity_id:
            return None
        contract_id = f"{ticker}:{outcome_mint}"
        return {
            "activityId": f"dflow:{activity_id}",
            "proxyWallet": identity,
            "asset": contract_id,
            "contract_id": contract_id,
            "market_id": self.market_id,
            "side": order_side,
            "size": size,
            "price": price,
            "timestamp": int(timestamp),
            "transactionHash": transaction,
            "slug": ticker,
            "outcome": outcome,
            "source": "dflow_onchain_trades",
            "wallet": identity,
            "trade_id": activity_id,
            "raw": dict(raw),
        }

    def _metadata_get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.runtime.get_json(
            self._url(self.metadata_api_base_url, path),
            params=params,
            headers=self._api_headers(),
        )

    def _api_headers(self) -> Dict[str, str]:
        credential = self.resolve_credential("dflow_api_key", ("DFLOW_API_KEY",), label="DFLOW_API_KEY")
        return {"x-api-key": credential.value} if credential else {}

    def _trade_request(self, order: PaperOrderRequest, outcome_mint: str) -> Dict[str, Any]:
        user_public_key = str(
            order.metadata.get("user_public_key")
            or order.metadata.get("userPublicKey")
            or self.config.get("dflow_wallet_address")
            or os.getenv("DFLOW_WALLET_ADDRESS")
            or os.getenv("DFLOW_USER_PUBLIC_KEY")
            or ""
        ).strip()
        if not user_public_key:
            user_public_key = "PAPER_WALLET_NOT_PROVIDED"
        buy = str(order.side or "").upper() == "BUY"
        raw_amount = order.metadata.get("amount_raw")
        if raw_amount is None:
            raw_amount = round(float(order.size) * float(order.metadata.get("amount_scale") or 1_000_000))
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("DFlow amount_raw must be an integer number of base units.") from exc
        if amount <= 0:
            raise MarketConfigurationError("DFlow order amount must be positive.")
        return {
            "inputMint": self.settlement_mint if buy else outcome_mint,
            "outputMint": outcome_mint if buy else self.settlement_mint,
            "amount": str(amount),
            "userPublicKey": user_public_key,
        }

    def _resolve_contract(self, contract_id: str) -> Tuple[Mapping[str, Any], str, str, str]:
        market_id, reference = self._split_contract_id(contract_id)
        market = self._market_cache.get(market_id)
        if market is None:
            if reference.upper() not in {"YES", "NO"}:
                payload = self._metadata_get(f"/api/v1/market/by-mint/{quote(reference, safe='')}")
                market = self._mapping_payload(payload)
            if not market:
                payload = self._metadata_get("/api/v1/markets", params={"ticker": market_id, "limit": 1})
                rows = self._rows(payload, "markets", "data")
                market = rows[0] if rows else None
            if market:
                self._cache_market(market)
        if not market:
            raise MarketConfigurationError(f"DFlow market {market_id!r} was not found.")
        account, _settlement = self._account_for_market(market)
        yes_mint = self._mint(account, "yesMint", market.get("yesMint"))
        no_mint = self._mint(account, "noMint", market.get("noMint"))
        ref_upper = reference.upper()
        if ref_upper == "YES":
            mint, side = yes_mint, "YES"
        elif ref_upper == "NO":
            mint, side = no_mint, "NO"
        elif reference == yes_mint:
            mint, side = yes_mint, "YES"
        elif reference == no_mint:
            mint, side = no_mint, "NO"
        else:
            raise MarketConfigurationError(f"DFlow market {market_id!r} has no outcome mint {reference!r}.")
        if not mint:
            raise MarketConfigurationError(f"DFlow market {market_id!r} has no {side} outcome mint.")
        return market, mint, side, f"{self._market_id(market)}:{mint}"

    def _event_from_payload(self, event: Mapping[str, Any]) -> MarketEvent:
        event_id = self._event_id(event)
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=str(event.get("title") or event.get("name") or event_id),
            url=str(event.get("url") or event.get("ticker") or ""),
            status=self._status(event),
            raw=dict(event),
        )

    def _contracts_from_market(self, market: Mapping[str, Any], *, event_id: str) -> List[MarketContract]:
        market_id = self._market_id(market)
        account, settlement = self._account_for_market(market)
        contracts: List[MarketContract] = []
        for side, mint_key, label_key in (("YES", "yesMint", "yesSubTitle"), ("NO", "noMint", "noSubTitle")):
            mint = self._mint(account, mint_key, market.get(mint_key))
            if not mint:
                continue
            label = str(market.get(label_key) or side.title())
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"{market_id}:{mint}",
                    event_id=str(market.get("eventTicker") or event_id),
                    title=f"{str(market.get('title') or market_id)} - {label}",
                    outcome=label,
                    url=str(market.get("url") or market_id),
                    status=self._status(market),
                    raw={"market": dict(market), "account": dict(account), "settlement_mint": settlement, "side": side},
                )
            )
        return contracts

    def _validate_order(self, order: PaperOrderRequest) -> None:
        self.ensure_order_market(order)
        self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in {"BUY", "SELL"}:
            raise MarketConfigurationError("DFlow order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("DFlow order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("DFlow order size must be positive and finite.")
        if order.limit_price is not None and self._safe_probability(order.limit_price) is None:
            raise MarketConfigurationError("DFlow order limit price must be between 0 and 1.")

    def _cache_market(self, market: Mapping[str, Any]) -> None:
        market_id = self._market_id(market)
        if market_id:
            self._market_cache[market_id] = market

    def _account_for_market(self, market: Mapping[str, Any]) -> Tuple[Mapping[str, Any], str]:
        accounts = market.get("accounts")
        if isinstance(accounts, Mapping):
            preferred = accounts.get(self.settlement_mint)
            if isinstance(preferred, Mapping) and (preferred.get("yesMint") or preferred.get("noMint")):
                return preferred, self.settlement_mint
            for settlement, account in accounts.items():
                if isinstance(account, Mapping) and (account.get("yesMint") or account.get("noMint")):
                    return account, str(settlement)
        return market, str(market.get("settlementMint") or self.settlement_mint)

    @staticmethod
    def _market_rows(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        value = payload.get("markets") if isinstance(payload, Mapping) else None
        return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    @staticmethod
    def _rows(payload: Any, *keys: str) -> List[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
            data = payload.get("data")
            if isinstance(data, Mapping):
                return DFlowAdapter._rows(data, *keys)
        return []

    @staticmethod
    def _mapping_payload(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            return dict(data) if isinstance(data, Mapping) else dict(payload)
        return {}

    @staticmethod
    def _market_id(market: Mapping[str, Any]) -> str:
        return str(market.get("ticker") or market.get("marketTicker") or market.get("id") or "").strip()

    @staticmethod
    def _event_id(event: Mapping[str, Any]) -> str:
        return str(event.get("ticker") or event.get("eventTicker") or event.get("id") or "").strip()

    @staticmethod
    def _status(payload: Mapping[str, Any]) -> str:
        return str(payload.get("status") or payload.get("state") or "").strip().lower()

    @staticmethod
    def _search_text(payload: Mapping[str, Any]) -> str:
        values = (payload.get("ticker"), payload.get("title"), payload.get("subtitle"), payload.get("name"))
        return " ".join(str(value or "") for value in values).lower()

    @staticmethod
    def _mint(account: Mapping[str, Any], key: str, fallback: Any = None) -> str:
        return str(account.get(key) or fallback or "").strip()

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        raw = str(contract_id or "").strip()
        if ":" not in raw:
            raise MarketConfigurationError("DFlow contract id must be MARKET_TICKER:MINT or MARKET_TICKER:YES|NO.")
        market_id, reference = raw.rsplit(":", 1)
        if not market_id.strip() or not reference.strip():
            raise MarketConfigurationError("DFlow contract id must be MARKET_TICKER:MINT or MARKET_TICKER:YES|NO.")
        return market_id.strip(), reference.strip()

    @staticmethod
    def _levels(raw: Any, *, descending: bool = False) -> List[OrderBookLevel]:
        rows = [[price, size] for price, size in raw.items()] if isinstance(raw, Mapping) else raw
        if not isinstance(rows, list):
            return []
        levels: List[OrderBookLevel] = []
        for item in rows:
            if isinstance(item, Mapping):
                price, size = item.get("price"), item.get("size") or item.get("quantity")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                price, size = item[0], item[1]
            else:
                continue
            try:
                price_value, size_value = float(price), float(size)
            except (TypeError, ValueError):
                continue
            if math.isfinite(price_value) and math.isfinite(size_value) and 0 <= price_value <= 1 and size_value > 0:
                levels.append(OrderBookLevel(price=price_value, size=size_value))
        levels.sort(key=lambda level: level.price, reverse=descending)
        return levels

    @classmethod
    def _complement_levels(cls, raw: Any) -> List[OrderBookLevel]:
        levels = cls._levels(raw)
        return sorted(
            [OrderBookLevel(price=1.0 - level.price, size=level.size) for level in levels if 0 <= level.price <= 1],
            key=lambda level: level.price,
        )

    @staticmethod
    def _safe_probability(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and 0 <= parsed <= 1 else None

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("DFlow history limit must be an integer between 1 and 1000.") from exc
        if parsed < 1 or parsed > 1000:
            raise MarketConfigurationError("DFlow history limit must be between 1 and 1000.")
        return parsed

    @staticmethod
    def _activity_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("DFlow activity limit must be an integer between 1 and 250.") from exc
        if parsed < 1 or parsed > 250:
            raise MarketConfigurationError("DFlow activity limit must be between 1 and 250.")
        return parsed

    @staticmethod
    def _activity_cursor(value: Any) -> str:
        """Validate the opaque pagination token without interpreting it."""

        cursor = str(value or "").strip()
        if len(cursor) > 256 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cursor):
            raise MarketConfigurationError("DFlow activity cursor must be a printable token up to 256 characters.")
        return cursor

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"DFlow {label} timestamp must be numeric.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"DFlow {label} timestamp must be a finite non-negative number.")
        return int(parsed)

    @staticmethod
    def _candle_interval(resolution: str) -> int:
        intervals = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }
        normalized = str(resolution or "").strip().lower()
        try:
            return intervals[normalized]
        except KeyError as exc:
            raise MarketConfigurationError(
                f"DFlow candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("dflow_candle_trade_limit", 1000)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("DFlow candle trade limit must be an integer between 1 and 1000.") from exc
        if limit < 1 or limit > 1000:
            raise MarketConfigurationError("DFlow candle trade limit must be between 1 and 1000.")
        return limit

    @classmethod
    def _trade_price(cls, raw: Mapping[str, Any], outcome: str) -> Optional[float]:
        keys = (("yesPrice", "yesPriceDollars") if outcome == "YES" else ("noPrice", "noPriceDollars")) + (
            "price",
            "priceDollars",
        )
        for key in keys:
            price = cls._safe_probability(raw.get(key))
            if price is not None:
                return price
        return None

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0:
            return None
        return parsed / 1000.0 if parsed > 10_000_000_000 else parsed

    @staticmethod
    def _url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{str(path or '').strip('/')}"
