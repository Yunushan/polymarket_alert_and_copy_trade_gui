from __future__ import annotations

import base64
import binascii
import math
import re
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError
from .identity import require_activity_identity
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_METADAO_API_BASE_URL = "https://market-api.metadao.fi"
METADAO_REFERENCES = (
    "https://api-docs.metadao.fi/introduction",
    "https://api-docs.metadao.fi/api-reference/get-api-tickers",
    "https://api-docs.metadao.fi/configuration",
    "https://github.com/metaDAOproject/futarchy-external-api",
    "https://github.com/metaDAOproject/futarchy-external-api/blob/89862a22abe1cf11d98804318901941f566d2fca/README.md#dexscreener-adapter-endpoints",
    "https://github.com/metaDAOproject/futarchy-external-api/blob/89862a22abe1cf11d98804318901941f566d2fca/src/routes/dexscreener.ts",
)

_SOLANA_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SOLANA_INDEX = {character: index for index, character in enumerate(_SOLANA_ALPHABET)}
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$")
_SOLANA_SIGNATURE_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,128}$")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_DEXSCREENER_SLOT_SPAN = 500_000
_MAX_HISTORY_WINDOWS = 50
_MAX_HISTORY_EVENTS = 250_000
_DEFAULT_HISTORY_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_HISTORY_RESPONSE_BYTES = 64 * 1024 * 1024
_CANDLE_INTERVALS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


def _decode_solana_address(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _SOLANA_ADDRESS_RE.fullmatch(text):
        raise MarketConfigurationError(f"MetaDAO {label} must be a canonical base58 public key.")
    number = 0
    try:
        for character in text:
            number = number * 58 + _SOLANA_INDEX[character]
    except KeyError as exc:
        raise MarketConfigurationError(f"MetaDAO {label} contains an invalid base58 character.") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(text) - len(text.lstrip("1"))
    if len((b"\x00" * leading_zeroes) + raw) != 32:
        raise MarketConfigurationError(f"MetaDAO {label} must decode to exactly 32 bytes.")
    return text


def _decode_solana_signature(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _SOLANA_SIGNATURE_RE.fullmatch(text):
        raise MarketConfigurationError(f"MetaDAO {label} must be a canonical base58 signature.")
    number = 0
    try:
        for character in text:
            number = number * 58 + _SOLANA_INDEX[character]
    except KeyError as exc:
        raise MarketConfigurationError(
            f"MetaDAO {label} contains an invalid base58 character."
        ) from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(text) - len(text.lstrip("1"))
    if len((b"\x00" * leading_zeroes) + raw) != 64:
        raise MarketConfigurationError(f"MetaDAO {label} must decode to exactly 64 bytes.")
    return text


class MetaDAOAdapter(MarketAdapter):
    """Public MetaDAO Futarchy DEX ticker adapter.

    MetaDAO's current official API is a public CoinGecko-compatible feed of
    DAO/token pairs. It exposes prices, bid/ask summaries, volume, liquidity,
    and a slot-bounded public spot-swap tape, but not depth quantities or a
    user-order endpoint. The adapter maps those documented rows to the shared market
    model, derives bounded candles from exact swap legs, and keeps paper orders
    local. Swap rows include the documented maker wallet, so bounded wallet
    activity can be filtered locally for simulation-first copy previews.
    The documented Futarchy API also exposes a configurable Solana router for
    swaps; live forwarding is limited to an operator-reviewed, externally
    signed transaction targeted at an explicit router allow-list. The adapter
    never signs, approves tokens, or settles positions.
    """

    metadata = get_market_metadata("metadao")
    live_order_sides = ("BUY", "SELL")
    account_recovery_operations = ("activity",)

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        (
            history_slot_window,
            history_max_windows,
            history_event_cap,
            history_response_byte_cap,
        ) = self._history_scan_limits()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(METADAO_REFERENCES),
                "public_api": True,
                "rate_limit_per_minute": 60,
                "live_trading_supported": bool(self.capabilities.live_trading),
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "signed_transaction_submission_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("metadao_submit_signed_transactions", False),
                "rpc_configured": bool(self._configured_rpc_url),
                "allowlisted_router_program_count": len(self.router_program_ids),
                "wallet_transaction_required": True,
                "trade_history_source": "public_futarchyamm_spot_swaps",
                "trade_history_coverage": "bounded_recent",
                "copy_trading_supported": True,
                "copy_activity_source": "public_futarchyamm_spot_swaps",
                "copy_activity_coverage": "bounded_recent",
                "account_recovery_operations": list(self.account_recovery_operations),
                "public_account_endpoints": [
                    "GET /dexscreener/events (bounded slot scans; local maker filter)",
                ],
                "activity_ticker_limit": self._activity_ticker_limit(),
                "activity_trade_scan_limit": self._activity_trade_scan_limit(),
                "history_slot_window": history_slot_window,
                "history_max_windows": history_max_windows,
                "history_event_cap": history_event_cap,
                "history_response_byte_cap": history_response_byte_cap,
                "candle_history_derived": True,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("metadao_api_base_url") or self.config.get("api_base_url")
        base = str(configured or DEFAULT_METADAO_API_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("MetaDAO API base URL must be an absolute http(s) URL without query or fragment.")
        return base

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        rows = self._tickers()
        needle = str(query or "").strip().lower()
        if needle:
            rows = [row for row in rows if needle in self._search_text(row)]
        events: List[MarketEvent] = []
        for row in rows[:desired]:
            ticker_id = self._ticker_id(row)
            if ticker_id:
                events.append(self._event_from_row(row, ticker_id))
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        event_key = self._required_ticker_id(event_id)
        row = self._find_ticker(event_key)
        if not row:
            raise MarketConfigurationError(f"MetaDAO ticker {event_key!r} was not found.")
        title = self._title(row, event_key)
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(event_key),
                event_id=event_key,
                title=title,
                outcome=str(self._value(row, "base_symbol", "base_name") or "BASE"),
                url=f"{self.api_base_url}/api/tickers",
                status="open",
                raw=dict(row),
            )
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        ticker_id = self._split_contract_id(contract_id)
        row = self._find_ticker(ticker_id)
        if not row:
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} was not found.")
        last = self._positive_number(self._value(row, "last_price", "lastPrice"))
        bid = self._positive_number(self._value(row, "bid", "highest_bid"))
        ask = self._positive_number(self._value(row, "ask", "lowest_ask"))
        midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else last or bid or ask
        if last is None:
            last = midpoint
        if last is None:
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} did not return a usable price.")
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(ticker_id),
            last=last,
            bid=bid,
            ask=ask,
            midpoint=midpoint,
            source="metadao_futarchy_dex_api",
            raw={"ticker": dict(row)},
        )

    def list_activity(self, wallet_address: str, *, limit: int = 25) -> List[Dict[str, Any]]:
        """Return bounded public swap activity for a Solana wallet.

        MetaDAO's official DexScreener-compatible swap rows include ``maker``
        and a stable transaction/event identity.  The endpoint is global and
        slot-ranged rather than wallet-ranged, so this method scans a bounded
        recent slice for a bounded number of published tickers, filters makers
        locally, and never claims complete wallet history.
        """

        self.ensure_capability("copy_trading")
        identity = require_activity_identity(self.market_id, wallet_address)
        wallet = identity.split(":", 1)[1]
        desired = self._activity_limit(limit)
        ticker_limit = self._activity_ticker_limit()
        scan_limit = self._activity_trade_scan_limit()
        rows = self._tickers()
        activities: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for ticker in rows[:ticker_limit]:
            ticker_id = self._ticker_id(ticker)
            if not ticker_id:
                continue
            contract_id = self._contract_id(ticker_id)
            scan = self._scan_public_trades(
                contract_id,
                before=None,
                after=None,
                desired_limit=scan_limit,
                require_creation_coverage=False,
                require_desired_after_cutoff=False,
            )
            for trade in scan["trades"]:
                raw = trade.raw if isinstance(trade.raw, Mapping) else {}
                maker = str(raw.get("maker") or "").strip()
                if maker != wallet:
                    continue
                identity_key = (trade.contract_id, trade.trade_id)
                if identity_key in seen:
                    continue
                seen.add(identity_key)
                event = raw.get("event") if isinstance(raw.get("event"), Mapping) else {}
                activities.append(
                    {
                        "activityId": f"metadao:{trade.trade_id}",
                        "proxyWallet": identity,
                        "asset": trade.contract_id,
                        "contract_id": trade.contract_id,
                        "market_id": self.market_id,
                        "side": trade.side,
                        "size": trade.size,
                        "price": trade.price,
                        "timestamp": int(trade.timestamp or 0),
                        "transactionHash": str(event.get("txnId") or "").strip(),
                        "slug": ticker_id,
                        "outcome": str(self._value(ticker, "base_symbol", "base_name") or "BASE"),
                        "source": "metadao_dexscreener_spot_swaps",
                        "maker": maker,
                        "trade_id": trade.trade_id,
                        "raw": dict(raw),
                    }
                )

        activities.sort(key=lambda row: (int(row.get("timestamp") or 0), str(row.get("activityId") or "")), reverse=True)
        return activities[:desired]

    def account_recovery(self, operation: str, **kwargs: Any) -> Dict[str, Any]:
        """Read MetaDAO's bounded public maker-activity feed.

        The shared account surface uses an explicit allow-list even though
        this is a public wallet-scoped read.  The upstream endpoint is a
        slot-ranged global tape, so the returned rows are intentionally a
        bounded recent slice rather than a complete account history.
        """

        normalized = str(operation or "").strip().lower()
        if normalized not in self.account_recovery_operations:
            raise MarketConfigurationError(
                "MetaDAO account operation must be one of: "
                + ", ".join(self.account_recovery_operations)
                + "."
            )
        self.ensure_capability("copy_trading")
        identity = require_activity_identity(
            self.market_id,
            kwargs.get("wallet") or kwargs.get("address"),
        )
        desired = self._activity_limit(kwargs.get("limit", 25))
        activities = self.list_activity(identity, limit=desired)
        return {
            "source": "metadao_dexscreener_spot_swaps",
            "endpoint": "/dexscreener/events",
            "wallet": identity,
            "limit": desired,
            "coverage": "bounded_recent",
            "activity": activities,
            "raw": activities,
        }

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return recent public FutarchyAMM spot swaps from bounded slot scans.

        MetaDAO's official DexScreener endpoint is slot-ranged rather than
        cursor- or timestamp-ranged.  Calls therefore walk backwards from the
        latest indexed slot in finite, non-overlapping windows.  Caller-supplied
        timestamp bounds fail closed unless the scan proves it reached the
        requested boundary (or the pair's creation slot).
        """

        self.ensure_capability("trade_history")
        desired = self._history_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("MetaDAO trade history requires before to be at or after after.")

        scan = self._scan_public_trades(
            contract_id,
            before=before_ts,
            after=after_ts,
            desired_limit=desired,
            # Estimated Solana block timestamps are not guaranteed to be
            # monotonic. Any lower timestamp bound therefore requires a scan
            # through the pair's creation slot before completeness is claimed.
            require_creation_coverage=after_ts is not None,
            require_desired_after_cutoff=before_ts is not None and after_ts is None,
        )
        trades = sorted(scan["trades"], key=self._trade_order_key, reverse=True)
        return trades[:desired]

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive deterministic bounded OHLCV candles from public spot swaps."""

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        requested_start = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else None
        )
        end_ts = (
            self._history_timestamp(to_timestamp, "to_timestamp")
            if to_timestamp is not None
            else None
        )
        if requested_start is not None and end_ts is not None and end_ts < requested_start:
            raise MarketConfigurationError(
                "MetaDAO candle history requires to_timestamp to be at or after from_timestamp."
            )

        # A timestamp inside a candle cannot prove the earlier part of that
        # bucket. Start at the next boundary so the first emitted bucket is
        # complete; an already-aligned timestamp remains inclusive.
        candle_start: Optional[float] = None
        if requested_start is not None:
            candle_start = float(math.ceil(requested_start / interval) * interval)
            if end_ts is not None and candle_start > end_ts:
                self._validate_history_identity_and_limits(contract_id)
                return []

        scan = self._scan_public_trades(
            contract_id,
            before=end_ts,
            after=candle_start,
            desired_limit=None,
            require_creation_coverage=requested_start is not None or end_ts is not None,
            require_desired_after_cutoff=False,
        )
        chronological = sorted(scan["trades"], key=self._trade_order_key)
        buckets: Dict[int, Dict[str, Any]] = {}
        for trade in chronological:
            if trade.timestamp is None:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
            if candle_start is not None and bucket_timestamp < int(candle_start):
                continue
            bucket = buckets.setdefault(
                bucket_timestamp,
                {
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                    "volume": 0.0,
                    "trade_ids": [],
                },
            )
            bucket["high"] = max(float(bucket["high"]), trade.price)
            bucket["low"] = min(float(bucket["low"]), trade.price)
            bucket["close"] = trade.price
            volume = float(bucket["volume"]) + trade.size
            if not math.isfinite(volume):
                raise MarketConfigurationError("MetaDAO derived candle volume must remain finite.")
            bucket["volume"] = volume
            bucket["trade_ids"].append(trade.trade_id)

        scan_truncated = not bool(scan["reached_creation"])
        if buckets and scan_truncated:
            earliest_bucket = min(buckets)
            del buckets[earliest_bucket]

        canonical = str(scan["contract_id"])
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
                    "source": "metadao_dexscreener_spot_swaps",
                    "derived": True,
                    "resolution": str(resolution).strip().lower(),
                    "pair_id": scan["pair_id"],
                    "trade_ids": list(bucket["trade_ids"]),
                    "scanned_from_slot": scan["scanned_from_slot"],
                    "scanned_to_slot": scan["scanned_to_slot"],
                    "reached_creation": bool(scan["reached_creation"]),
                    "history_coverage": (
                        "bounded_slot_slice" if scan_truncated else "complete_pair_scan"
                    ),
                    "partial": scan_truncated,
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        """Return MetaDAO's documented one-level spread-adjusted quote view.

        The official ticker feed publishes bid/ask summaries, but no depth or
        quote quantities. Unknown sizes are represented as ``0.0`` and the
        original ticker row remains attached for auditability; this deliberately
        does not claim a full orderbook.
        """

        self.ensure_capability("orderbook_reading")
        ticker_id = self._split_contract_id(contract_id)
        row = self._find_ticker(ticker_id)
        if not row:
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} was not found.")
        bid = self._positive_number(self._value(row, "bid", "highest_bid"))
        ask = self._positive_number(self._value(row, "ask", "lowest_ask"))
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(ticker_id),
            bids=[OrderBookLevel(price=bid, size=0.0)] if bid is not None else [],
            asks=[OrderBookLevel(price=ask, size=0.0)] if ask is not None else [],
            raw={
                "ticker": dict(row),
                "depth": "top_of_book_only",
                "size_available": False,
            },
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        ticker_id = self._validate_order(order)
        price = self._positive_number(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(ticker_id)).last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(ticker_id),
            accepted=True,
            message=(
                f"DRY RUN: would place MetaDAO {str(order.side).upper()} for {float(order.size):.4f} token units"
                + (f" at price {float(price):.8f}" if price is not None else "")
            ),
            raw={
                "dry_run": True,
                "request": {
                    "ticker_id": ticker_id,
                    "side": str(order.side).upper(),
                    "size": float(order.size),
                    "limit_price": price,
                },
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        ticker_id = self._validate_order(order)
        audit = self.preflight_live_order(order, feature_name="MetaDAO live trading")
        if not self.config_bool("metadao_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "MetaDAO live trading requires metadao_submit_signed_transactions=true after reviewing the signed router transaction."
            )
        rpc_url = self._configured_rpc_url
        if not rpc_url:
            raise MarketConfigurationError(
                "MetaDAO live orders require an explicit metadao_solana_rpc_url or solana_rpc_url for transaction submission."
            )
        allowlisted = self.router_program_ids
        if not allowlisted:
            raise MarketConfigurationError(
                "MetaDAO live orders require at least one explicitly reviewed metadao_router_program_ids entry."
            )
        row = self._find_ticker(ticker_id)
        if not row:
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} was not found.")
        metadata = dict(order.metadata or {})
        signed = str(
            metadata.get("signed_transaction") or metadata.get("signedTransaction") or ""
        ).strip()
        raw = self._decode_signed_transaction(signed)
        router = _decode_solana_address(
            metadata.get("router_program_id") or metadata.get("program_id"), label="router program id"
        )
        if not any(router.casefold() == address.casefold() for address in allowlisted):
            raise MarketConfigurationError(
                "MetaDAO signed transaction metadata targets a program outside the reviewed router allow-list."
            )
        reviewed_ticker = str(metadata.get("ticker_id") or metadata.get("market_id") or "").strip()
        if reviewed_ticker != ticker_id:
            raise MarketConfigurationError("MetaDAO signed transaction metadata targets a different ticker.")
        expected_pool = str(self._value(row, "pool_id", "poolId") or "").strip()
        if expected_pool and str(metadata.get("pool_id") or "").strip() != expected_pool:
            raise MarketConfigurationError("MetaDAO signed transaction metadata targets a different pool.")
        instruction = str(metadata.get("instruction") or metadata.get("method") or "").strip().lower()
        if instruction not in {"swap", "buy", "sell"}:
            raise MarketConfigurationError("MetaDAO live orders require reviewed swap/buy/sell instruction metadata.")
        side = str(order.side or "").upper()
        if (side == "BUY" and instruction == "sell") or (side == "SELL" and instruction == "buy"):
            raise MarketConfigurationError("MetaDAO instruction metadata does not match the requested order side.")
        instruction_data = str(metadata.get("instruction_data") or metadata.get("data") or "").strip()
        if not instruction_data:
            raise MarketConfigurationError("MetaDAO live orders require reviewed instruction_data metadata.")
        signature = self._solana_rpc(
            rpc_url,
            "sendTransaction",
            [signed, {"encoding": "base64", "skipPreflight": False}],
        )
        try:
            signature = _decode_solana_signature(
                signature, label="RPC transaction signature"
            )
        except MarketConfigurationError as exc:
            raise MarketHTTPError(
                "MetaDAO RPC did not return a valid transaction signature."
            ) from exc
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(ticker_id),
            "live": True,
            "preflight": audit,
            "submission": "solana_rpc_sendTransaction",
            "signature": signature,
            "router_program_id": router,
            "ticker_id": ticker_id,
            "instruction": instruction,
            "signed_transaction_bytes": len(raw),
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        identity = require_activity_identity(
            self.market_id,
            activity.get("proxyWallet") or activity.get("proxy_wallet") or activity.get("wallet"),
        )
        raw = activity.get("raw") if isinstance(activity.get("raw"), Mapping) else {}
        maker = str(activity.get("maker") or raw.get("maker") or "").strip()
        if maker and identity != f"solana:{maker}":
            raise MarketConfigurationError("MetaDAO activity maker does not match proxyWallet.")
        contract_id = str(activity.get("asset") or activity.get("contract_id") or "").strip()
        if not contract_id:
            raise MarketConfigurationError("MetaDAO activity has no contract id.")
        side = str(activity.get("side") or "").strip().upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("MetaDAO activity side must be BUY or SELL.")
        size = self._strict_positive_number(activity.get("size"), "activity size")
        price = self._strict_positive_number(activity.get("price"), "activity price")
        return self.place_paper_order(
            PaperOrderRequest(
                market_id=self.market_id,
                contract_id=contract_id,
                side=side,
                size=size,
                limit_price=price,
                metadata={
                    "activity": dict(activity),
                    "source": "metadao_public_maker_swaps",
                    "activity_identity": identity,
                },
            )
        )

    def _scan_public_trades(
        self,
        contract_id: str,
        *,
        before: Optional[float],
        after: Optional[float],
        desired_limit: Optional[int],
        require_creation_coverage: bool,
        require_desired_after_cutoff: bool,
    ) -> Dict[str, Any]:
        ticker_id = self._split_contract_id(contract_id)
        canonical = self._contract_id(ticker_id)
        ticker = self._find_ticker(ticker_id)
        if not ticker:
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} was not found.")

        pool_id = _decode_solana_address(
            self._value(ticker, "pool_id", "poolId"), label="history pool id"
        )
        base_mint = _decode_solana_address(
            self._value(ticker, "base_currency", "baseCurrency"), label="history base mint"
        )
        quote_mint = _decode_solana_address(
            self._value(ticker, "target_currency", "targetCurrency"), label="history quote mint"
        )
        if ticker_id != f"{base_mint}_{quote_mint}":
            raise MarketConfigurationError(
                "MetaDAO ticker identity does not match its documented base/quote mint pair."
            )

        pair_payload = self.runtime.get_json(
            self._url("/dexscreener/pair"), params={"id": pool_id}, headers={}
        )
        if not isinstance(pair_payload, Mapping) or not isinstance(pair_payload.get("pair"), Mapping):
            raise MarketConfigurationError("MetaDAO /dexscreener/pair returned an unsupported payload shape.")
        pair = pair_payload["pair"]
        if str(pair.get("id") or "").strip() != pool_id:
            raise MarketConfigurationError("MetaDAO history pair id does not match the ticker pool id.")
        if str(pair.get("asset0Id") or "").strip() != base_mint:
            raise MarketConfigurationError("MetaDAO history pair asset0 does not match the ticker base mint.")
        if str(pair.get("asset1Id") or "").strip() != quote_mint:
            raise MarketConfigurationError("MetaDAO history pair asset1 does not match the ticker quote mint.")
        # Do not use ``dexKey`` as an identity field. MetaDAO's pinned route
        # implementation currently emits ``futarchyAMM`` while its official
        # README example uses ``futarchy``. The immutable pool/base/quote keys
        # above provide the exact attribution needed by this adapter.
        created_slot = self._positive_safe_integer(
            pair.get("createdAtBlockNumber"), "pair createdAtBlockNumber"
        )
        created_timestamp = self._positive_safe_integer(
            pair.get("createdAtBlockTimestamp"), "pair createdAtBlockTimestamp"
        )
        created_transaction_id = _decode_solana_signature(
            pair.get("createdAtTxnId"), label="pair createdAtTxnId"
        )

        latest_payload = self.runtime.get_json(
            self._url("/dexscreener/latest-block"), params=None, headers={}
        )
        if not isinstance(latest_payload, Mapping) or not isinstance(latest_payload.get("block"), Mapping):
            raise MarketConfigurationError(
                "MetaDAO /dexscreener/latest-block returned an unsupported payload shape."
            )
        latest = latest_payload["block"]
        latest_slot = self._positive_safe_integer(latest.get("blockNumber"), "latest blockNumber")
        self._positive_safe_integer(
            latest.get("blockTimestamp"), "latest blockTimestamp"
        )
        if latest_slot < created_slot:
            raise MarketConfigurationError("MetaDAO latest indexed block predates the selected pair.")

        slot_span, max_windows, event_cap, response_byte_cap = self._history_scan_limits()

        trades_by_id: Dict[str, MarketTrade] = {}
        seen_trade_ids: set[str] = set()
        raw_event_count = 0
        reached_creation = False
        saw_creation_event = False
        scanned_from_slot = latest_slot
        scanned_to_slot = latest_slot
        to_slot = latest_slot

        for _window_index in range(max_windows):
            from_slot = max(created_slot, to_slot - slot_span)
            payload = self.runtime.get_json(
                self._url("/dexscreener/events"),
                params={"fromBlock": from_slot, "toBlock": to_slot},
                headers={},
                max_response_bytes=response_byte_cap,
            )
            if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
                raise MarketConfigurationError(
                    "MetaDAO /dexscreener/events returned an unsupported payload shape."
                )
            rows = payload["events"]
            raw_event_count += len(rows)
            if raw_event_count > event_cap:
                raise MarketConfigurationError(
                    "MetaDAO history event cap was exceeded before the requested range was complete."
                )

            scanned_from_slot = min(scanned_from_slot, from_slot)
            for raw_event in rows:
                if not isinstance(raw_event, Mapping):
                    continue
                if str(raw_event.get("pairId") or "").strip() != pool_id:
                    # This endpoint is global. Rows for other pairs neither
                    # contribute history nor prove timestamp coverage, and
                    # their schema is outside this pair's trust boundary.
                    continue
                trade = self._trade_from_event(
                    raw_event,
                    contract_id=canonical,
                    pair_id=pool_id,
                    ticker=ticker,
                    pair=pair,
                    from_slot=from_slot,
                    to_slot=to_slot,
                )
                if int(trade.raw["block_number"]) == created_slot:
                    event_transaction_id = str(raw_event.get("txnId") or "").strip()
                    if event_transaction_id == created_transaction_id:
                        if int(float(trade.timestamp or 0)) != created_timestamp:
                            raise MarketConfigurationError(
                                "MetaDAO pair creation event timestamp does not match pair metadata."
                            )
                        saw_creation_event = True
                if trade.trade_id in seen_trade_ids:
                    raise MarketConfigurationError(
                        "MetaDAO history returned a duplicate swap event identifier."
                    )
                seen_trade_ids.add(trade.trade_id)
                if before is not None and (trade.timestamp is None or trade.timestamp > before):
                    continue
                if after is not None and (trade.timestamp is None or trade.timestamp < after):
                    continue
                trades_by_id[trade.trade_id] = trade

            reached_creation = from_slot == created_slot
            if reached_creation:
                break
            if not require_creation_coverage and desired_limit is not None:
                if len(trades_by_id) >= desired_limit:
                    break
            to_slot = from_slot - 1

        if require_creation_coverage and not reached_creation:
            raise MarketConfigurationError(
                "MetaDAO bounded slot scan could not prove timestamp coverage through pair creation."
            )
        if reached_creation and not saw_creation_event:
            raise MarketConfigurationError(
                "MetaDAO creation-slot history did not contain the pair's declared first swap."
            )
        if (
            require_desired_after_cutoff
            and desired_limit is not None
            and len(trades_by_id) < desired_limit
            and not reached_creation
        ):
            raise MarketConfigurationError(
                "MetaDAO bounded slot scan could not prove the requested number of trades before the cutoff."
            )

        return {
            "contract_id": canonical,
            "pair_id": pool_id,
            "trades": list(trades_by_id.values()),
            "reached_creation": reached_creation,
            "scanned_from_slot": scanned_from_slot,
            "scanned_to_slot": scanned_to_slot,
        }

    def _trade_from_event(
        self,
        event: Mapping[str, Any],
        *,
        contract_id: str,
        pair_id: str,
        ticker: Mapping[str, Any],
        pair: Mapping[str, Any],
        from_slot: int,
        to_slot: int,
    ) -> MarketTrade:
        if str(event.get("eventType") or "").strip().lower() != "swap":
            raise MarketConfigurationError("MetaDAO history event type must be swap.")
        if str(event.get("pairId") or "").strip() != pair_id:
            raise MarketConfigurationError("MetaDAO history event targets a different pair.")

        buy_keys_present = event.get("asset1In") not in (None, "") or event.get("asset0Out") not in (None, "")
        sell_keys_present = event.get("asset0In") not in (None, "") or event.get("asset1Out") not in (None, "")
        if buy_keys_present == sell_keys_present:
            raise MarketConfigurationError("MetaDAO history event must contain exactly one BUY or SELL leg pair.")
        if buy_keys_present:
            quote_amount = self._strict_positive_number(event.get("asset1In"), "event asset1In")
            size = self._strict_positive_number(event.get("asset0Out"), "event asset0Out")
            side = "BUY"
        else:
            size = self._strict_positive_number(event.get("asset0In"), "event asset0In")
            quote_amount = self._strict_positive_number(event.get("asset1Out"), "event asset1Out")
            side = "SELL"

        price = self._strict_positive_number(event.get("priceNative"), "event priceNative")
        calculated_price = quote_amount / size
        if not math.isclose(price, calculated_price, rel_tol=1e-9, abs_tol=1e-12):
            raise MarketConfigurationError("MetaDAO history event priceNative does not match its swap legs.")

        block = event.get("block")
        if not isinstance(block, Mapping):
            raise MarketConfigurationError("MetaDAO history event block must be an object.")
        block_number = self._nonnegative_integer(block.get("blockNumber"), "event blockNumber")
        if block_number < from_slot or block_number > to_slot:
            raise MarketConfigurationError(
                "MetaDAO history event blockNumber falls outside its requested slot window."
            )
        timestamp = float(
            self._positive_safe_integer(block.get("blockTimestamp"), "event blockTimestamp")
        )
        transaction_index = self._nonnegative_integer(event.get("txnIndex"), "event txnIndex")
        event_index = self._nonnegative_integer(event.get("eventIndex"), "event eventIndex")
        transaction_id = _decode_solana_signature(
            event.get("txnId"), label="history event txnId"
        )
        maker = _decode_solana_address(event.get("maker"), label="history event maker")
        trade_id = f"{transaction_id}:{transaction_index}:{event_index}"
        return MarketTrade(
            market_id=self.market_id,
            contract_id=contract_id,
            trade_id=trade_id,
            side=side,
            price=price,
            size=size,
            timestamp=timestamp,
            raw={
                "source": "metadao_dexscreener_spot_swaps",
                "event": dict(event),
                "ticker": dict(ticker),
                "pair": dict(pair),
                "pair_id": pair_id,
                "block_number": block_number,
                "transaction_index": transaction_index,
                "event_index": event_index,
                "maker": maker,
                "quote_size": quote_amount,
            },
        )

    @staticmethod
    def _trade_order_key(trade: MarketTrade) -> tuple[int, int, int, str, float]:
        raw = trade.raw if isinstance(trade.raw, Mapping) else {}
        return (
            int(raw.get("block_number") or 0),
            int(raw.get("transaction_index") or 0),
            int(raw.get("event_index") or 0),
            trade.trade_id,
            float(trade.timestamp if trade.timestamp is not None else -1),
        )

    @staticmethod
    def _history_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("MetaDAO trade history limit must be an integer.")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError("MetaDAO trade history limit must be an integer.") from exc
        if number < 1 or number > 500 or str(number) != str(value).strip():
            raise MarketConfigurationError("MetaDAO trade history limit must be between 1 and 500.")
        return number

    @staticmethod
    def _activity_limit(value: Any) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError("MetaDAO activity limit must be an integer.")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError("MetaDAO activity limit must be an integer.") from exc
        if number < 1 or number > 100 or str(number) != str(value).strip():
            raise MarketConfigurationError("MetaDAO activity limit must be between 1 and 100.")
        return number

    def _activity_ticker_limit(self) -> int:
        return self._bounded_int_config("metadao_activity_ticker_limit", 100, minimum=1, maximum=100)

    def _activity_trade_scan_limit(self) -> int:
        return self._bounded_int_config("metadao_activity_trade_scan_limit", 100, minimum=1, maximum=500)

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative Unix timestamp.")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative Unix timestamp.") from exc
        if not math.isfinite(number) or number < 0:
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative Unix timestamp.")
        return number

    @staticmethod
    def _nonnegative_integer(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative safe integer.")
        if isinstance(value, int):
            number = value
        elif isinstance(value, float):
            if not math.isfinite(value) or not value.is_integer():
                raise MarketConfigurationError(
                    f"MetaDAO {label} must be a non-negative safe integer."
                )
            number = int(value)
        else:
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative safe integer.")
        if number < 0 or number > _MAX_SAFE_INTEGER:
            raise MarketConfigurationError(f"MetaDAO {label} must be a non-negative safe integer.")
        return number

    @staticmethod
    def _strict_positive_number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MarketConfigurationError(f"MetaDAO {label} must be positive and finite.")
        try:
            number = float(value)
        except OverflowError as exc:
            raise MarketConfigurationError(
                f"MetaDAO {label} must be positive and finite."
            ) from exc
        if not math.isfinite(number) or number <= 0:
            raise MarketConfigurationError(f"MetaDAO {label} must be positive and finite.")
        return number

    @classmethod
    def _positive_safe_integer(cls, value: Any, label: str) -> int:
        number = cls._nonnegative_integer(value, label)
        if number == 0:
            raise MarketConfigurationError(f"MetaDAO {label} must be a positive safe integer.")
        return number

    def _bounded_int_config(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            raise MarketConfigurationError(f"MetaDAO config {key} must be an integer.")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError(f"MetaDAO config {key} must be an integer.") from exc
        if str(number) != str(value).strip() or number < minimum or number > maximum:
            raise MarketConfigurationError(
                f"MetaDAO config {key} must be between {minimum} and {maximum}."
            )
        return number

    def _history_scan_limits(self) -> tuple[int, int, int, int]:
        return (
            self._bounded_int_config(
                "metadao_history_slot_window",
                500_000,
                minimum=1,
                maximum=_MAX_DEXSCREENER_SLOT_SPAN,
            ),
            self._bounded_int_config(
                "metadao_history_max_windows",
                3,
                minimum=1,
                maximum=_MAX_HISTORY_WINDOWS,
            ),
            self._bounded_int_config(
                "metadao_history_event_cap",
                50_000,
                minimum=1,
                maximum=_MAX_HISTORY_EVENTS,
            ),
            self._bounded_int_config(
                "metadao_history_response_byte_cap",
                _DEFAULT_HISTORY_RESPONSE_BYTES,
                minimum=1_024,
                maximum=_MAX_HISTORY_RESPONSE_BYTES,
            ),
        )

    @staticmethod
    def _candle_interval(resolution: str) -> int:
        key = str(resolution or "").strip().lower()
        interval = _CANDLE_INTERVALS.get(key)
        if interval is None:
            supported = ", ".join(_CANDLE_INTERVALS)
            raise MarketConfigurationError(f"MetaDAO candle resolution must be one of: {supported}.")
        return interval

    def _tickers(self) -> List[Mapping[str, Any]]:
        payload = self.runtime.get_json(self._url("/api/tickers"), params=None, headers={})
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("data", "tickers", "result"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, Mapping)]
        raise MarketConfigurationError("MetaDAO /api/tickers returned an unsupported payload shape.")

    def _find_ticker(self, ticker_id: str) -> Mapping[str, Any]:
        matches = [row for row in self._tickers() if self._ticker_id(row) == ticker_id]
        if len(matches) > 1:
            raise MarketConfigurationError(
                f"MetaDAO ticker {ticker_id!r} is ambiguous across multiple pools."
            )
        return matches[0] if matches else {}

    def _validate_history_identity_and_limits(self, contract_id: str) -> None:
        self._history_scan_limits()
        ticker_id = self._split_contract_id(contract_id)
        if not self._find_ticker(ticker_id):
            raise MarketConfigurationError(f"MetaDAO ticker {ticker_id!r} was not found.")

    def _event_from_row(self, row: Mapping[str, Any], ticker_id: str) -> MarketEvent:
        return MarketEvent(
            market_id=self.market_id,
            event_id=ticker_id,
            title=self._title(row, ticker_id),
            url=f"{self.api_base_url}/api/tickers",
            status="open",
            raw=dict(row),
        )

    @classmethod
    def _ticker_id(cls, row: Mapping[str, Any]) -> str:
        value = cls._value(row, "ticker_id", "tickerId", "id")
        return str(value).strip() if value not in (None, "") else ""

    @classmethod
    def _title(cls, row: Mapping[str, Any], ticker_id: str) -> str:
        base = str(cls._value(row, "base_symbol", "base_name") or "BASE")
        quote = str(cls._value(row, "target_symbol", "target_name") or "QUOTE")
        return f"{base}/{quote} ({ticker_id})"

    @classmethod
    def _search_text(cls, row: Mapping[str, Any]) -> str:
        return " ".join(
            str(cls._value(row, key) or "")
            for key in ("ticker_id", "base_currency", "target_currency", "base_symbol", "base_name", "target_symbol", "target_name", "pool_id")
        ).lower()

    def _validate_order(self, order: PaperOrderRequest) -> str:
        self.ensure_order_market(order)
        ticker_id = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("MetaDAO order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MarketConfigurationError("MetaDAO order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("MetaDAO order size must be positive and finite.")
        if order.limit_price is not None and self._positive_number(order.limit_price) is None:
            raise MarketConfigurationError("MetaDAO order limit price must be positive and finite.")
        return ticker_id

    @staticmethod
    def _contract_id(ticker_id: str) -> str:
        return f"{ticker_id}:0"

    @staticmethod
    def _split_contract_id(contract_id: Any) -> str:
        text = str(contract_id or "").strip()
        parts = text.rsplit(":", 1)
        if len(parts) != 2 or parts[1] != "0":
            raise MarketConfigurationError("MetaDAO contract id must be '<ticker_id>:0'.")
        return MetaDAOAdapter._required_ticker_id(parts[0])

    @staticmethod
    def _required_ticker_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text or any(char in text for char in "\\/?#%") or len(text) > 200:
            raise MarketConfigurationError("MetaDAO ticker id is invalid.")
        return text

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    @staticmethod
    def _value(payload: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return None

    def _url(self, path: str) -> str:
        if path not in {
            "/api/tickers",
            "/dexscreener/latest-block",
            "/dexscreener/pair",
            "/dexscreener/events",
        }:
            raise MarketConfigurationError("MetaDAO request path is not an approved official endpoint.")
        return f"{self.api_base_url}{path}"

    @property
    def _configured_rpc_url(self) -> str:
        configured = self.config.get("metadao_solana_rpc_url") or self.config.get("solana_rpc_url")
        if not configured:
            return ""
        value = str(configured).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("MetaDAO Solana RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def router_program_ids(self) -> tuple[str, ...]:
        configured = self.config.get("metadao_router_program_ids")
        if configured in (None, ""):
            return ()
        values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
        addresses: List[str] = []
        for value in values:
            address = _decode_solana_address(value, label="router program id")
            if address.casefold() not in {item.casefold() for item in addresses}:
                addresses.append(address)
        return tuple(addresses)

    @staticmethod
    def _decode_signed_transaction(value: str) -> bytes:
        if not value or len(value) > 1_400_000 or len(value) % 4:
            raise MarketConfigurationError("MetaDAO live orders require a canonical base64 wallet-signed transaction.")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MarketConfigurationError("MetaDAO live orders require a canonical base64 wallet-signed transaction.") from exc
        if len(raw) < 64 or len(raw) > 1_000_000:
            raise MarketConfigurationError("MetaDAO signed transaction has an invalid size.")
        if base64.b64encode(raw).decode("ascii") != value:
            raise MarketConfigurationError("MetaDAO signed transaction must use canonical base64 encoding.")
        return raw

    def _solana_rpc(self, url: str, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("MetaDAO RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"MetaDAO RPC error: {payload['error']}")
        return payload.get("result")
