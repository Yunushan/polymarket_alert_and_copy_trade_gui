from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, UnsupportedFeatureError
from .types import (
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
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
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
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "DFlow copy trading is unsupported because wallet activity mirroring is outside the official API contract.",
        )

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
    def _history_timestamp(value: Any, label: str) -> int:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"DFlow {label} timestamp must be numeric.") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MarketConfigurationError(f"DFlow {label} timestamp must be a finite non-negative number.")
        return int(parsed)

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

