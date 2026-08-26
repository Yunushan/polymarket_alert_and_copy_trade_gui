from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


DEFAULT_DRIFT_BET_API_BASE_URL = "https://data.api.drift.trade"
DRIFT_BET_REFERENCES = (
    "https://data.api.drift.trade/playground/json",
    "https://github.com/drift-labs/protocol-v2/tree/master/sdk",
    "https://www.mintlify.com/drift-labs/protocol-v2/api/markets/perp-markets",
)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OUTCOMES = ("YES", "NO")


class DriftBetAdapter(MarketAdapter):
    """Read-only/paper adapter for Drift Protocol BET markets.

    Drift's public Data API documents BET prediction records but does not
    currently publish a stable market-list endpoint in its OpenAPI contract.
    The inventory is therefore explicit configuration (or the environment
    variable ``DRIFT_BET_MARKET_SYMBOLS``).  This avoids scraping private
    endpoints or silently treating every perp market as a binary event.

    Live execution remains disabled: actual orders require Solana/Drift SDK
    wallet signing, collateral, settlement, and chain-specific safeguards.
    """

    metadata = get_market_metadata("drift_bet")
    live_order_sides = ("BUY", "SELL")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        specs = self._market_specs(allow_empty=True)
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(DRIFT_BET_REFERENCES),
                "public_api": True,
                "anonymous_read_access": True,
                "configured_market_symbols": [spec["symbol"] for spec in specs],
                "configured_market_count": len(specs),
                "dynamic_discovery": False,
                "inventory_notice": (
                    "Configure drift_bet_market_symbols; the public OpenAPI contract exposes per-symbol BET "
                    "records but no stable market-list endpoint."
                ),
                "trade_history_source": "public_prediction_fill_records",
                "history_retention_days": 31,
                "history_page_limit": 20,
                "candle_history_derived": True,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "wallet_transaction_required": True,
                "collateral_required": True,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("drift_bet_data_api_base_url") or self.config.get("api_base_url")
        base = str(configured or DEFAULT_DRIFT_BET_API_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError(
                "Drift BET Data API base URL must be an absolute http(s) URL without query or fragment."
            )
        return base

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        specs = self._market_specs()
        desired = max(1, min(int(limit or 50), 100))
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for spec in specs:
            title = str(spec["title"])
            if needle and needle not in f"{spec['symbol']} {title}".lower():
                continue
            records = self._predictions(spec["symbol"])
            raw = {"inventory": dict(spec), "prediction_records": records}
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=self._event_id(spec["symbol"]),
                    title=title,
                    url=f"{self.api_base_url}/market/{spec['symbol']}/predictions",
                    status=str(spec.get("status") or self._status(records)),
                    raw=raw,
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        symbol = self._symbol_from_event_id(event_id)
        spec = self._spec_for_symbol(symbol)
        records = self._predictions(symbol)
        title = str(spec["title"])
        status = str(spec.get("status") or self._status(records))
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(symbol, outcome),
                event_id=self._event_id(symbol),
                title=f"{title} - {outcome}",
                outcome=outcome,
                url=f"{self.api_base_url}/market/{symbol}/predictions",
                status=status,
                raw={"inventory": dict(spec), "prediction_records": records},
            )
            for outcome in _OUTCOMES
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        symbol, outcome = self._split_contract_id(contract_id)
        records = self._predictions(symbol)
        if not records:
            raise MarketHTTPError(f"Drift BET returned no prediction records for {symbol}.")
        latest = records[0]
        yes_price = self._prediction_price(latest)
        if yes_price is None:
            raise MarketHTTPError(
                f"Drift BET prediction record for {symbol} did not expose a bounded binary price."
            )
        price = yes_price if outcome == "YES" else 1.0 - yes_price
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(symbol, outcome),
            last=price,
            bid=price,
            ask=price,
            midpoint=price,
            source="drift_data_api_predictions",
            raw={"record": dict(latest), "yes_price": yes_price, "outcome": outcome},
        )

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return bounded public fills from Drift's BET prediction feed.

        The official endpoint is newest-first, returns at most 20 records, and
        retains the latest 31 days.  ``before``/``after`` are therefore local
        filters over that bounded page rather than claims of unbounded history.
        Taker LONG/SHORT direction is expressed as BUY/SELL for YES and
        inverted for the complementary NO contract.
        """

        self.ensure_capability("trade_history")
        symbol, outcome = self._split_contract_id(contract_id)
        self._spec_for_symbol(symbol)
        desired = self._history_limit(limit)
        before_ts = self._history_timestamp(before, "before") if before is not None else None
        after_ts = self._history_timestamp(after, "after") if after is not None else None
        if before_ts is not None and after_ts is not None and before_ts < after_ts:
            raise MarketConfigurationError("Drift BET trade history requires before to be at or after after.")

        canonical = self._contract_id(symbol, outcome)
        trades: List[MarketTrade] = []
        for record in self._predictions(symbol):
            row_symbol = str(record.get("symbol") or "").strip()
            if row_symbol and row_symbol.lower() != symbol.lower():
                continue
            market_type = str(record.get("marketType") or "").strip().lower()
            # BET contracts are prediction-contract perp markets.  Some older
            # fixtures/consumers used a prediction-specific label, so accept
            # those aliases while retaining the protocol's real ``perp`` form.
            if market_type and market_type not in {"perp", "bet", "prediction"}:
                continue
            action = str(record.get("action") or "").strip().lower()
            if action and action != "fill":
                continue
            timestamp = self._timestamp_seconds(record.get("ts"))
            if timestamp is None:
                continue
            if before_ts is not None and timestamp > before_ts:
                continue
            if after_ts is not None and timestamp < after_ts:
                continue
            yes_price = self._prediction_price(record)
            size = self._prediction_size(record)
            trade_id = self._prediction_trade_id(record)
            yes_side = self._prediction_side(record)
            if yes_price is None or size is None or not trade_id or yes_side is None:
                continue
            side = yes_side if outcome == "YES" else ("SELL" if yes_side == "BUY" else "BUY")
            price = yes_price if outcome == "YES" else 1.0 - yes_price
            trades.append(
                MarketTrade(
                    market_id=self.market_id,
                    contract_id=canonical,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=timestamp,
                    raw={
                        "record": dict(record),
                        "yes_price": yes_price,
                        "yes_side": yes_side,
                        "outcome": outcome,
                        "source": "drift_data_api_predictions",
                        "retention_days": 31,
                    },
                )
            )
            if len(trades) >= desired:
                break
        return trades

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Derive bounded OHLCV candles from Drift's documented BET fills."""

        self.ensure_capability("candle_history")
        interval = self._candle_interval(resolution)
        start_ts = (
            self._history_timestamp(from_timestamp, "from_timestamp")
            if from_timestamp is not None
            else None
        )
        end_ts = self._history_timestamp(to_timestamp, "to_timestamp") if to_timestamp is not None else None
        if start_ts is not None and end_ts is not None and end_ts < start_ts:
            raise MarketConfigurationError(
                "Drift BET candle history requires to_timestamp to be at or after from_timestamp."
            )

        trades = self.list_trades(
            contract_id,
            limit=self._candle_trade_limit(),
            before=end_ts,
            after=start_ts,
        )
        buckets: Dict[int, Dict[str, Any]] = {}
        indexed_trades = list(enumerate(trades))
        for _source_index, trade in sorted(indexed_trades, key=self._candle_trade_order_key):
            if trade.timestamp is None:
                continue
            bucket_timestamp = int(float(trade.timestamp) // interval * interval)
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
            bucket["volume"] += max(0.0, float(trade.size))
            bucket["trade_ids"].append(trade.trade_id)

        symbol, outcome = self._split_contract_id(contract_id)
        canonical = self._contract_id(symbol, outcome)
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
                    "source": "drift_data_api_predictions",
                    "derived": True,
                    "retention_days": 31,
                    "resolution": str(resolution or "").strip().lower(),
                    "interval_seconds": interval,
                    "trade_ids": list(bucket["trade_ids"]),
                },
            )
            for bucket_timestamp, bucket in sorted(buckets.items())
        ]

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "The documented Drift Data API provides BET prediction records, but this adapter does not infer a binary orderbook from DLOB/WS payloads.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        symbol, outcome = self._validate_order(order)
        price = self._probability(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(symbol, outcome)).last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(symbol, outcome),
            accepted=True,
            message=(
                f"DRY RUN: would place Drift BET {str(order.side).upper()} for {float(order.size):.4f} "
                f"{outcome} shares on {symbol}"
                + (f" at probability {float(price):.4f}" if price is not None else "")
            ),
            filled_size=0.0,
            average_price=price,
            raw={
                "dry_run": True,
                "official_api_is_read_only": True,
                "request": {
                    "symbol": symbol,
                    "outcome": outcome,
                    "side": str(order.side).upper(),
                    "size": float(order.size),
                    "limit_price": price,
                },
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Drift BET live trading requires Solana/Drift SDK wallet signing, collateral, settlement, and chain safeguards; only public read and dry-run surfaces are implemented.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Drift BET copy trading is unsupported because the public Data API does not provide a safe account-activity mirroring contract.",
        )

    def _predictions(self, symbol: str) -> List[Mapping[str, Any]]:
        payload = self._get(f"/market/{symbol}/predictions")
        records = self._records(payload)
        return [dict(record) for record in records if isinstance(record, Mapping)]

    def _get(self, path: str, *, params: Optional[Mapping[str, Any]] = None) -> Any:
        segments = [segment for segment in str(path or "").split("/") if segment]
        if any(segment in {".", ".."} or not _SYMBOL_RE.fullmatch(segment) for segment in segments):
            raise MarketConfigurationError("Drift BET API path contains an invalid segment.")
        return self.runtime.get_json(f"{self.api_base_url}/{'/'.join(segments)}", params=params, headers={})

    def _market_specs(self, *, allow_empty: bool = False) -> List[Dict[str, Any]]:
        configured: Any = self.config.get("drift_bet_market_symbols")
        if configured in (None, ""):
            configured = self.config.get("drift_bet_markets")
        if configured in (None, ""):
            configured = os.getenv("DRIFT_BET_MARKET_SYMBOLS", "")
        if isinstance(configured, Mapping):
            configured = [configured]
        elif isinstance(configured, str):
            configured = [item.strip() for item in configured.split(",") if item.strip()]
        elif configured is None:
            configured = []
        elif not isinstance(configured, (list, tuple, set)):
            raise MarketConfigurationError("Drift BET market symbols must be a list, mapping, or comma-separated string.")

        specs: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in configured:
            if isinstance(item, Mapping):
                symbol = self._validate_symbol(item.get("symbol"))
                spec = dict(item)
                spec["symbol"] = symbol
                spec["title"] = str(item.get("title") or symbol)
                if item.get("status") not in (None, ""):
                    spec["status"] = str(item["status"]).lower()
            else:
                symbol = self._validate_symbol(item)
                spec = {"symbol": symbol, "title": symbol}
            if symbol.lower() in seen:
                continue
            seen.add(symbol.lower())
            specs.append(spec)
        if not specs and not allow_empty:
            raise MarketConfigurationError(
                "Drift BET requires explicit drift_bet_market_symbols (or DRIFT_BET_MARKET_SYMBOLS); "
                "the documented public API has no stable market-list endpoint."
            )
        return specs

    def _spec_for_symbol(self, symbol: str) -> Dict[str, Any]:
        for spec in self._market_specs():
            if spec["symbol"].lower() == symbol.lower():
                return spec
        raise MarketConfigurationError(f"Drift BET symbol {symbol!r} is not in configured market inventory.")

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, str]:
        self.ensure_order_market(order)
        symbol, outcome = self._split_contract_id(order.contract_id)
        self._spec_for_symbol(symbol)
        side = str(order.side or "").upper()
        if side not in self.live_order_sides:
            raise MarketConfigurationError("Drift BET order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Drift BET order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Drift BET order size must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Drift BET order limit price must be between 0 and 1.")
        return symbol, outcome

    @staticmethod
    def _records(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            for key in ("records", "predictions", "data", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        return []

    @classmethod
    def _prediction_price(cls, record: Mapping[str, Any]) -> Optional[float]:
        for key in ("yesPrice", "yes_price", "probability", "predictionPrice", "prediction_price", "price"):
            price = cls._probability(record.get(key))
            if price is not None:
                return price
        try:
            quote = abs(float(record.get("quoteAssetAmountFilled")))
            base = abs(float(record.get("baseAssetAmountFilled")))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(quote) or not math.isfinite(base) or base <= 0:
            return None
        # Drift's protocol records use BASE_PRECISION=1e9 and
        # QUOTE_PRECISION=1e6.  Apply those units explicitly; magnitude-based
        # detection is ambiguous for small raw fills and large formatted ones.
        ratio = (quote / 1_000_000.0) / (base / 1_000_000_000.0)
        if 0 < ratio < 1:
            return ratio
        return None

    @staticmethod
    def _prediction_size(record: Mapping[str, Any]) -> Optional[float]:
        try:
            base = abs(float(record.get("baseAssetAmountFilled")))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(base) or base <= 0:
            return None
        size = base / 1_000_000_000.0
        return size if math.isfinite(size) and size > 0 else None

    @staticmethod
    def _candle_trade_order_key(item: Tuple[int, MarketTrade]) -> Tuple[float, float, float, int]:
        """Order newest-first feed rows chronologically for OHLC derivation."""

        source_index, trade = item
        record = trade.raw.get("record") if isinstance(trade.raw, Mapping) else None
        row = record if isinstance(record, Mapping) else {}

        def numeric(value: Any) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return -1.0
            return parsed if math.isfinite(parsed) else -1.0

        # The endpoint is newest-first.  Timestamp, slot, and event index give
        # chronological ordering when present; reverse source position is the
        # deterministic fallback for otherwise indistinguishable rows.
        return (
            float(trade.timestamp or 0.0),
            numeric(row.get("slot")),
            numeric(row.get("txSigIndex")),
            -source_index,
        )

    @staticmethod
    def _prediction_side(record: Mapping[str, Any]) -> Optional[str]:
        direction = str(record.get("takerOrderDirection") or "").strip().lower()
        if direction in {"long", "buy"}:
            return "BUY"
        if direction in {"short", "sell"}:
            return "SELL"
        return None

    @staticmethod
    def _prediction_trade_id(record: Mapping[str, Any]) -> str:
        fill_id = str(record.get("fillRecordId") or "").strip()
        if fill_id:
            return fill_id
        signature = str(record.get("txSig") or "").strip()
        index = record.get("txSigIndex")
        return f"{signature}:{index}" if signature and index not in (None, "") else ""

    @staticmethod
    def _history_limit(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Drift BET trade limit must be an integer.") from exc
        return max(1, min(parsed, 20))

    @staticmethod
    def _history_timestamp(value: Any, label: str) -> float:
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Drift BET {label} timestamp must be numeric epoch seconds.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(
                f"Drift BET {label} timestamp must be a finite non-negative epoch second."
            )
        return timestamp

    @staticmethod
    def _timestamp_seconds(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(timestamp) or timestamp < 0:
            return None
        return timestamp / 1000.0 if timestamp > 10_000_000_000 else timestamp

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
                f"Drift BET candle resolution must be one of: {', '.join(intervals)}."
            ) from exc

    def _candle_trade_limit(self) -> int:
        raw_limit = self.config.get("drift_bet_candle_trade_limit", 20)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(
                "Drift BET candle trade limit must be an integer between 1 and 20."
            ) from exc
        if limit < 1 or limit > 20:
            raise MarketConfigurationError("Drift BET candle trade limit must be between 1 and 20.")
        return limit

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not 0 < number < 1:
            return None
        return number

    @staticmethod
    def _validate_symbol(value: Any) -> str:
        symbol = str(value or "").strip()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise MarketConfigurationError("Drift BET market symbols must contain only letters, numbers, '.', '_' or '-'.")
        return symbol

    @classmethod
    def _event_id(cls, symbol: str) -> str:
        return f"drift:{cls._validate_symbol(symbol)}"

    @classmethod
    def _contract_id(cls, symbol: str, outcome: str) -> str:
        normalized = str(outcome or "").strip().upper()
        if normalized not in _OUTCOMES:
            raise MarketConfigurationError("Drift BET outcome must be YES or NO.")
        return f"{cls._validate_symbol(symbol)}:{normalized}"

    @classmethod
    def _symbol_from_event_id(cls, event_id: Any) -> str:
        text = str(event_id or "").strip()
        if text.lower().startswith("drift:"):
            text = text.split(":", 1)[1]
        return cls._validate_symbol(text)

    @classmethod
    def _split_contract_id(cls, contract_id: Any) -> Tuple[str, str]:
        text = str(contract_id or "").strip()
        parts = text.rsplit(":", 1)
        if len(parts) != 2 or parts[1].upper() not in _OUTCOMES:
            raise MarketConfigurationError("Drift BET contract id must be '<symbol>:YES' or '<symbol>:NO'.")
        return cls._validate_symbol(parts[0]), parts[1].upper()

    @staticmethod
    def _status(records: Iterable[Mapping[str, Any]]) -> str:
        first = next(iter(records), {})
        value = first.get("status") or first.get("state")
        return str(value).lower() if value not in (None, "") else "active"
