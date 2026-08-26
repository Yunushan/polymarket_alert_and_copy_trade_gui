from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_IEM_MARKET_DATA_URL = (
    "https://iemweb.biz.uiowa.edu/historicaldata/uspoliticalmarkets/1996elections/"
    "1996powellnomination_wta/powellpricedata.txt"
)
IEM_REFERENCES = (
    "https://iemweb.biz.uiowa.edu/historicaldata/",
    "https://iemweb.biz.uiowa.edu/historicaldata/uspoliticalmarkets/1996elections/1996powellnomination_wta/1996powellnomination_wta_fileformat.txt",
    "https://iem.uiowa.edu/",
)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IEM_ALLOWED_HOSTS = {"iemweb.biz.uiowa.edu", "iem.uiowa.edu"}
_DEFAULT_MARKETS: Tuple[Dict[str, Any], ...] = (
    {
        "market_id": "1996-powell-nomination",
        "title": "1996 Powell Nomination Winner Takes All",
        "status": "closed",
        "data_url": DEFAULT_IEM_MARKET_DATA_URL,
        "contracts": {
            "P.YES": "Powell's name is placed in nomination",
            "P.NO": "Powell's name is not placed in nomination",
        },
    },
)


class IowaElectronicMarketsAdapter(MarketAdapter):
    """Archive-only adapter for Iowa Electronic Markets' official price files.

    IEM publishes a documented historical text-file format, but its current
    quote pages do not constitute a supported public API.  This adapter keeps
    the inventory explicit and only exposes normalized daily archive candles,
    latest archived prices, alerts, and local paper-order previews.  It never
    scrapes quote pages or submits IEM orders.
    """

    metadata = get_market_metadata("iowa_electronic_markets")
    live_order_sides = ("BUY", "SELL")

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        specs = self._market_specs()
        health.update(
            {
                "references": list(IEM_REFERENCES),
                "public_api": False,
                "official_archive": True,
                "archive_only": True,
                "configured_market_ids": [spec["market_id"] for spec in specs],
                "configured_market_count": len(specs),
                "dynamic_discovery": False,
                "quote_api_supported": False,
                "orderbook_supported": False,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = self._limit(limit, "IEM event limit", maximum=100)
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        for spec in self._market_specs():
            search_text = f"{spec['market_id']} {spec['title']}".lower()
            if needle and needle not in search_text:
                continue
            market_id = str(spec["market_id"])
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=self._event_id(market_id),
                    title=str(spec["title"]),
                    url=str(spec["data_url"]),
                    status=str(spec.get("status") or "archived").lower(),
                    raw={"inventory": dict(spec), "archive_only": True},
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        spec = self._spec_for_market(self._market_from_event_id(event_id))
        market_id = str(spec["market_id"])
        status = str(spec.get("status") or "archived").lower()
        contracts = spec["contracts"]
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(market_id, symbol),
                event_id=self._event_id(market_id),
                title=f"{spec['title']} - {title}",
                outcome=str(title),
                url=str(spec["data_url"]),
                status=status,
                raw={"inventory": dict(spec), "symbol": symbol, "title": title},
            )
            for symbol, title in contracts.items()
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, symbol = self._split_contract_id(contract_id)
        candles = self.list_candles(self._contract_id(market_id, symbol), resolution="1d")
        if not candles:
            raise MarketHTTPError(f"IEM archive contains no traded prices for {market_id}:{symbol}.")
        latest = max(candles, key=lambda candle: candle.timestamp)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, symbol),
            last=latest.close,
            bid=None,
            ask=None,
            midpoint=latest.close,
            source="iem_historical_price_file",
            raw={"candle": latest.raw, "archive_only": True},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "IEM publishes historical price files but no supported public orderbook API for this adapter.",
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1d",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        market_id, symbol = self._split_contract_id(contract_id)
        if str(resolution or "").strip().lower() not in {"1d", "day", "daily"}:
            raise MarketConfigurationError("IEM archive resolution must be 1d, day, or daily.")
        start = self._timestamp_bound(from_timestamp, "from") if from_timestamp is not None else None
        end = self._timestamp_bound(to_timestamp, "to") if to_timestamp is not None else None
        if start is not None and end is not None and start > end:
            raise MarketConfigurationError("IEM archive candle range requires from_timestamp <= to_timestamp.")
        spec = self._spec_for_market(market_id)
        if symbol not in spec["contracts"]:
            raise MarketConfigurationError(f"IEM contract symbol {symbol!r} is not configured for {market_id!r}.")
        text = self._get_text(str(spec["data_url"]))
        candles: List[MarketCandle] = []
        for row in self._parse_rows(text):
            if row["symbol"] != symbol or row["last"] < 0:
                continue
            timestamp = row["timestamp"]
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=self._contract_id(market_id, symbol),
                    timestamp=timestamp,
                    # IEM publishes low/high/last but no session-open field;
                    # use the documented last price as the point's open rather
                    # than inventing an opening value from the daily low.
                    open=row["last"],
                    high=row["high"],
                    low=row["low"],
                    close=row["last"],
                    volume=row["dollar_volume"],
                    raw=dict(row["raw"]),
                )
            )
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        market_id, symbol = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("IEM paper order side must be BUY or SELL.")
        size = self._positive_number(order.size, "IEM paper order size")
        price = self._bounded_probability(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(market_id, symbol)).last
        if price is None:
            raise MarketHTTPError(f"IEM archive contains no price for {market_id}:{symbol}.")
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(market_id, symbol),
            accepted=True,
            message=(
                f"DRY RUN: would place IEM archive {str(order.side).upper()} for {size:g} "
                f"{symbol} at {price:.4f}; archive data is not a live order venue"
            ),
            average_price=price,
            raw={"dry_run": True, "archive_only": True, "official_api_is_read_only": True},
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "IEM's documented archive has no supported public order-submission API; live trading is disabled.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "IEM does not publish a supported account-activity feed for copy trading.",
        )

    def _get_text(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("IEM data_url must be an absolute http(s) URL without query or fragment.")
        allow_custom = self.config_bool("iem_allow_custom_data_host", False)
        if parsed.hostname not in _IEM_ALLOWED_HOSTS and not allow_custom:
            raise MarketConfigurationError(
                "IEM data_url must use iemweb.biz.uiowa.edu or iem.uiowa.edu unless "
                "iem_allow_custom_data_host is explicitly enabled for a test/archive mirror."
            )
        self.runtime.rate_limiter.wait()
        try:
            response = self.runtime.session.request(
                "GET", url, headers={}, timeout=self.runtime.timeout_seconds
            )
        except Exception as exc:
            raise MarketHTTPError(f"IEM historical data request failed: {exc}") from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            raise MarketHTTPError(f"IEM historical data HTTP {status}.")
        return str(getattr(response, "text", "") or "")

    def _market_specs(self) -> List[Dict[str, Any]]:
        configured: Any = self.config.get("iem_historical_markets")
        if configured in (None, ""):
            configured = [dict(spec) for spec in _DEFAULT_MARKETS]
        elif isinstance(configured, Mapping):
            configured = [configured]
        elif not isinstance(configured, (list, tuple)):
            raise MarketConfigurationError("iem_historical_markets must be a mapping or list of mappings.")
        specs: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in configured:
            if not isinstance(item, Mapping):
                raise MarketConfigurationError("Each IEM historical market must be a mapping.")
            market_id = self._safe_segment(item.get("market_id"), "IEM market_id")
            title = str(item.get("title") or market_id).strip()
            data_url = str(item.get("data_url") or "").strip()
            if not data_url:
                raise MarketConfigurationError(f"IEM market {market_id!r} requires data_url.")
            contracts = item.get("contracts")
            if not isinstance(contracts, Mapping) or not contracts:
                raise MarketConfigurationError(f"IEM market {market_id!r} requires a non-empty contracts mapping.")
            normalized_contracts: Dict[str, str] = {}
            for symbol, contract_title in contracts.items():
                clean_symbol = self._safe_symbol(symbol)
                normalized_contracts[clean_symbol] = str(contract_title or clean_symbol).strip()
            if market_id in seen:
                continue
            seen.add(market_id)
            specs.append(
                {
                    "market_id": market_id,
                    "title": title,
                    "status": str(item.get("status") or "archived").lower(),
                    "data_url": data_url,
                    "contracts": normalized_contracts,
                }
            )
        if not specs:
            raise MarketConfigurationError("IEM historical market inventory cannot be empty.")
        return specs

    def _spec_for_market(self, market_id: str) -> Dict[str, Any]:
        for spec in self._market_specs():
            if spec["market_id"] == market_id:
                return spec
        raise MarketConfigurationError(f"IEM market {market_id!r} is not in configured historical inventory.")

    @staticmethod
    def _parse_rows(text: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for line in str(text or "").splitlines():
            fields = line.split()
            if len(fields) < 9:
                continue
            try:
                date = _dt.datetime.strptime(fields[0], "%m/%d/%y").replace(tzinfo=_dt.timezone.utc)
                trades = int(fields[3])
                shares = float(fields[4])
                dollar_volume = float(fields[5])
                low = float(fields[6])
                high = float(fields[7])
                last = float(fields[8])
            except (TypeError, ValueError, OverflowError):
                continue
            if not all(math.isfinite(value) for value in (shares, dollar_volume, low, high, last)):
                continue
            if trades < 0 or shares < 0 or dollar_volume < 0:
                continue
            if last >= 0 and (not 0 <= low <= 1 or not 0 <= high <= 1 or not 0 <= last <= 1):
                continue
            rows.append(
                {
                    "date": fields[0],
                    "contract_number": fields[1],
                    "symbol": fields[2],
                    "trades": trades,
                    "shares": shares,
                    "dollar_volume": dollar_volume,
                    "low": low,
                    "high": high,
                    "last": last,
                    "timestamp": date.timestamp(),
                    "raw": {"fields": fields},
                }
            )
        return rows

    @staticmethod
    def _event_id(market_id: str) -> str:
        return f"archive:{market_id}"

    @staticmethod
    def _market_from_event_id(event_id: str) -> str:
        value = str(event_id or "").strip()
        if not value.startswith("archive:"):
            raise MarketConfigurationError("IEM event id must use the archive:<market_id> format.")
        return value.split(":", 1)[1]

    @staticmethod
    def _contract_id(market_id: str, symbol: str) -> str:
        return f"{market_id}:{symbol}"

    @staticmethod
    def _split_contract_id(contract_id: str) -> Tuple[str, str]:
        value = str(contract_id or "").strip()
        if value.count(":") != 1:
            raise MarketConfigurationError("IEM contract id must use the market_id:SYMBOL format.")
        market_id, symbol = value.split(":", 1)
        return (
            IowaElectronicMarketsAdapter._safe_segment(market_id, "IEM market_id"),
            IowaElectronicMarketsAdapter._safe_symbol(symbol),
        )

    @staticmethod
    def _safe_segment(value: Any, label: str) -> str:
        clean = str(value or "").strip()
        if not clean or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", clean):
            raise MarketConfigurationError(f"{label} must be a safe non-empty identifier.")
        return clean

    @staticmethod
    def _safe_symbol(value: Any) -> str:
        clean = str(value or "").strip()
        if not _SYMBOL_RE.fullmatch(clean):
            raise MarketConfigurationError("IEM contract symbols must be safe identifiers.")
        return clean

    @staticmethod
    def _limit(value: Any, label: str, *, maximum: int) -> int:
        try:
            desired = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be an integer between 1 and {maximum}.") from exc
        if desired < 1 or desired > maximum:
            raise MarketConfigurationError(f"{label} must be between 1 and {maximum}.")
        return desired

    @staticmethod
    def _timestamp_bound(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"IEM {label} timestamp must be numeric.") from exc
        if not math.isfinite(number):
            raise MarketConfigurationError(f"IEM {label} timestamp must be finite.")
        return number

    @staticmethod
    def _positive_number(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    @staticmethod
    def _bounded_probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or number > 1:
            return None
        return number
