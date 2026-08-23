from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketCandle, MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_SEER_API_BASE_URL = "https://app.seer.pm"
SEER_REFERENCES = (
    "https://seer-3.gitbook.io/seer-documentation/developers/interact-with-seer/api",
    "https://seer-3.gitbook.io/seer-documentation/developers/interact-with-seer/trading",
    "https://seer-3.gitbook.io/seer-documentation/developers/intro",
    "https://seer-3.gitbook.io/seer-documentation/developers/contracts",
    "https://github.com/seer-pm/demo/blob/main/web/netlify/functions/market-chart.mts",
    "https://github.com/seer-pm/demo/blob/main/web/src/hooks/chart/utils.ts",
    "https://github.com/seer-pm/demo/blob/main/packages/seer-pm-sdk/src/market-pools.ts",
)
SEER_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SeerAdapter(MarketAdapter):
    """Official Seer serverless API adapter for market data and paper orders.

    Seer documents public ``markets-search``, ``get-market``, and
    ``market-chart`` APIs which join its subgraph data with cached market
    metadata.  It exposes outcome odds, token addresses, and DEX-pool chart
    points, but it does not expose a CLOB orderbook or a signed-order
    submission endpoint; trading is performed through a third-party DEX using
    Seer outcome tokens.  The adapter therefore accepts only an
    operator-reviewed, externally signed EVM transaction targeted at an
    explicit DEX allow-list.  It never signs, approves collateral, or settles
    positions itself.
    """

    metadata = get_market_metadata("seer")
    live_order_sides = ("BUY", "SELL")

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._market_cache: Dict[str, Dict[str, Any]] = {}

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "api_base_url": self.api_base_url,
                "references": list(SEER_REFERENCES),
                "public_api": True,
                "candle_history_source": "market-chart",
                "candle_history_resolutions": ["raw", "price", "1h"],
                "live_trading_supported": True,
                "live_trading_enabled": self.config_bool("live_trading_enabled", False),
                "signed_transaction_submission_enabled": self.config_bool(
                    "seer_submit_signed_transactions", False
                ),
                "rpc_configured": bool(self._configured_rpc_url),
                "allowlisted_trading_contract_count": len(self.trading_contract_addresses),
                "wallet_transaction_required": True,
                "settlement_required": True,
            }
        )
        return health

    @property
    def api_base_url(self) -> str:
        configured = self.config.get("seer_api_base_url") or self.config.get("api_base_url")
        base = str(configured or DEFAULT_SEER_API_BASE_URL).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Seer API base URL must be an absolute http(s) URL without query or fragment.")
        return base

    @property
    def chain_ids(self) -> List[str]:
        configured = self.config.get("seer_chain_ids")
        if configured in (None, ""):
            return []
        values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
        chains: List[str] = []
        for value in values:
            text = str(value).strip()
            if not text.isdigit() or int(text) <= 0:
                raise MarketConfigurationError("Seer chain IDs must be positive decimal integers.")
            if text not in chains:
                chains.append(text)
        return chains

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 1000))
        body: Dict[str, Any] = {"limit": desired, "page": 1}
        needle = str(query or "").strip()
        if needle:
            body["marketName"] = needle
        chains = self.chain_ids
        if chains:
            body["chainsList"] = chains
        payload = self._post("/markets-search", body)
        rows = self._market_rows(payload)
        events: List[MarketEvent] = []
        self._market_cache = {}
        for row in rows[:desired]:
            market_id = self._address(row.get("id") or row.get("marketId"), label="market id")
            chain_id = self._chain_id(row.get("chainId"))
            event_id = self._event_id(chain_id, market_id)
            self._market_cache[event_id] = dict(row)
            events.append(self._event_from_row(row, event_id, chain_id, market_id))
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        chain_id, market_id = self._split_event_id(event_id)
        row = self._market_cache.get(self._event_id(chain_id, market_id))
        if not row:
            row = self._get_market(chain_id, market_id)
        outcomes = row.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            raise MarketConfigurationError(f"Seer market {market_id!r} did not return outcomes.")
        wrapped = row.get("wrappedTokens")
        wrapped_tokens = wrapped if isinstance(wrapped, list) else []
        contracts: List[MarketContract] = []
        for index, outcome in enumerate(outcomes):
            label = str(outcome or f"Outcome {index + 1}").strip()
            token = str(wrapped_tokens[index]).strip() if index < len(wrapped_tokens) else ""
            token_part = token if token else str(index)
            contract_id = self._contract_id(chain_id, market_id, index)
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=contract_id,
                    event_id=self._event_id(chain_id, market_id),
                    title=f"{self._title(row, market_id)} - {label}",
                    outcome=label,
                    url=self._market_url(row, market_id),
                    status=self._status(row),
                    raw={"market": dict(row), "outcome_index": index, "token_id": token_part},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        chain_id, market_id, outcome_index = self._split_contract_id(contract_id)
        row = self._market_cache.get(self._event_id(chain_id, market_id))
        if not row:
            row = self._get_market(chain_id, market_id)
        odds = row.get("odds")
        value: Any = odds[outcome_index] if isinstance(odds, list) and outcome_index < len(odds) else None
        price = self._probability(value)
        if price is None:
            raise MarketConfigurationError(f"Seer market {market_id!r} did not return a usable outcome price.")
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(chain_id, market_id, outcome_index),
            last=price,
            bid=price,
            ask=price,
            midpoint=price,
            source="seer_markets_search",
            raw={"market": dict(row), "outcome_index": outcome_index},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Seer's official API exposes market odds and charts, not a CLOB orderbook.",
        )

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return Seer's official DEX-pool chart points as flat candles.

        The Seer consumer selects the reciprocal price field according to the
        sorted outcome/collateral token pair: an outcome in ``token0`` uses
        ``token1Price`` and an outcome in ``token1`` uses ``token0Price``.
        The endpoint combines hourly pool snapshots with swap-time points, so
        this adapter preserves the irregular upstream timestamps and does not
        claim locally resampled OHLCV or volume.
        """

        self.ensure_capability("candle_history")
        requested_resolution = str(resolution or "1h").strip().lower()
        if requested_resolution not in {"raw", "price", "1h"}:
            raise MarketConfigurationError(
                "Seer chart history accepts resolution 'raw', 'price', or '1h'; "
                "the official mixed hourly/swap points are not resampled."
            )
        lower = self._history_timestamp_bound(from_timestamp, "from_timestamp")
        upper = self._history_timestamp_bound(to_timestamp, "to_timestamp")
        if lower is not None and upper is not None and lower > upper:
            raise MarketConfigurationError("Seer chart history requires from_timestamp <= to_timestamp.")

        chain_id, market_id, outcome_index = self._split_contract_id(contract_id)
        market = self._market_cache.get(self._event_id(chain_id, market_id))
        if not market:
            market = dict(self._get_market(chain_id, market_id))
        if str(market.get("type") or "").strip().casefold() != "generic":
            raise MarketConfigurationError(
                "Seer candle history currently supports only Generic markets; "
                "Futarchy pool-price semantics are different and remain fail-closed."
            )

        wrapped_tokens = market.get("wrappedTokens")
        if not isinstance(wrapped_tokens, list) or outcome_index >= len(wrapped_tokens):
            raise MarketConfigurationError(f"Seer market {market_id!r} did not return the requested outcome token.")
        outcome_token = self._address(wrapped_tokens[outcome_index], label="outcome token")
        collateral_token = self._address(market.get("collateralToken"), label="collateral token")
        if outcome_token.casefold() == collateral_token.casefold():
            raise MarketConfigurationError("Seer outcome and collateral tokens must be different addresses.")
        expected_token0, expected_token1 = sorted(
            (outcome_token, collateral_token),
            key=str.casefold,
        )

        payload = self._get(
            "/market-chart",
            params={"marketId": market_id, "chainId": int(chain_id)},
        )
        if not isinstance(payload, list):
            raise MarketConfigurationError("Seer market-chart must return one dataset per outcome.")
        if outcome_index >= len(payload):
            raise MarketConfigurationError(
                f"Seer market-chart omitted the dataset for outcome index {outcome_index}."
            )
        points = payload[outcome_index]
        if not isinstance(points, list):
            raise MarketConfigurationError("Seer market-chart outcome dataset must be a list.")

        canonical = self._contract_id(chain_id, market_id, outcome_index)
        candles: List[MarketCandle] = []
        for point in points:
            if not isinstance(point, Mapping):
                raise MarketConfigurationError("Seer market-chart points must be JSON objects.")
            timestamp = self._chart_timestamp(point.get("periodStartUnix"))
            pool = point.get("pool")
            if not isinstance(pool, Mapping):
                raise MarketConfigurationError("Seer market-chart point omitted its pool token pair.")
            pool_token0 = self._pool_token_address(pool.get("token0"), label="pool token0")
            pool_token1 = self._pool_token_address(pool.get("token1"), label="pool token1")
            if (
                pool_token0.casefold() != expected_token0.casefold()
                or pool_token1.casefold() != expected_token1.casefold()
            ):
                raise MarketConfigurationError(
                    "Seer market-chart pool tokens do not match the requested outcome/collateral pair."
                )

            token0_price, token1_price = self._chart_prices(point)
            if outcome_token.casefold() == pool_token0.casefold():
                price = token1_price
                price_field = "token1Price"
            else:
                price = token0_price
                price_field = "token0Price"
            if price is None or not 0 <= price <= 1:
                continue
            if lower is not None and timestamp < lower:
                continue
            if upper is not None and timestamp > upper:
                continue
            candles.append(
                MarketCandle(
                    market_id=self.market_id,
                    contract_id=canonical,
                    timestamp=timestamp,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=None,
                    raw={
                        "source": "seer_market_chart",
                        "flat_point": True,
                        "price_field": price_field,
                        "resolution_requested": requested_resolution,
                        "point": dict(point),
                    },
                )
            )
        candles.sort(key=lambda candle: candle.timestamp)
        return candles

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        chain_id, market_id, outcome_index = self._validate_order(order)
        price = self._probability(order.limit_price)
        if price is None:
            price = self.get_price(self._contract_id(chain_id, market_id, outcome_index)).last
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(chain_id, market_id, outcome_index),
            accepted=True,
            message=(
                f"DRY RUN: would place Seer {str(order.side).upper()} for {float(order.size):.4f} outcome units"
                + (f" at probability {float(price):.4f}" if price is not None else "")
            ),
            filled_size=0.0,
            average_price=price,
            raw={
                "dry_run": True,
                "request": {
                    "chain_id": chain_id,
                    "market_id": market_id,
                    "outcome_index": outcome_index,
                    "side": str(order.side).upper(),
                    "size": float(order.size),
                    "limit_price": price,
                },
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        chain_id, market_id, outcome_index = self._validate_order(order)
        audit = self.preflight_live_order(order, feature_name="Seer live trading")
        if not self.config_bool("seer_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "Seer live trading requires seer_submit_signed_transactions=true after reviewing the signed DEX transaction."
            )
        rpc_url = self._configured_rpc_url
        if not rpc_url:
            raise MarketConfigurationError(
                "Seer live orders require an explicit seer_rpc_url or evm_rpc_url for transaction submission."
            )
        allowlisted = self.trading_contract_addresses
        if not allowlisted:
            raise MarketConfigurationError(
                "Seer live orders require at least one explicitly reviewed seer_trading_contract_addresses entry."
            )
        metadata = dict(order.metadata or {})
        signed = str(
            metadata.get("signed_transaction") or metadata.get("signedTransaction") or ""
        ).strip()
        self._validate_signed_transaction(signed)
        target = str(metadata.get("transaction_to") or metadata.get("to") or "").strip()
        if not target or not any(target.casefold() == address.casefold() for address in allowlisted):
            raise MarketConfigurationError(
                "Seer signed transaction metadata targets a contract outside the reviewed DEX allow-list."
            )
        reviewed_chain = str(metadata.get("chain_id") or "").strip()
        if reviewed_chain != chain_id:
            raise MarketConfigurationError("Seer signed transaction metadata targets a different chain.")
        reviewed_market = str(metadata.get("market_address") or metadata.get("market_id") or "").strip()
        if reviewed_market.casefold() != market_id.casefold():
            raise MarketConfigurationError("Seer signed transaction metadata targets a different market.")
        try:
            reviewed_outcome = int(metadata.get("outcome_index"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Seer live orders require reviewed outcome_index metadata.") from exc
        if reviewed_outcome != outcome_index:
            raise MarketConfigurationError("Seer signed transaction metadata targets a different outcome.")
        method = str(metadata.get("method") or "").strip().lower()
        if method not in {"buy", "sell", "swap", "trade"}:
            raise MarketConfigurationError(
                "Seer live orders require reviewed buy/sell/swap/trade method metadata."
            )
        side = str(order.side or "").upper()
        if (side == "BUY" and method == "sell") or (side == "SELL" and method == "buy"):
            raise MarketConfigurationError("Seer signed transaction method does not match the requested order side.")
        data = str(metadata.get("data") or metadata.get("calldata") or "").strip()
        if data.startswith("0x"):
            data = data[2:]
        if not data or len(data) < 8 or len(data) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", data):
            raise MarketConfigurationError("Seer live orders require reviewed hexadecimal transaction calldata.")
        response = self._evm_rpc(rpc_url, "eth_sendRawTransaction", [signed])
        if not isinstance(response, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", response):
            raise MarketHTTPError("Seer RPC did not return a valid transaction hash.")
        return {
            "market_id": self.market_id,
            "contract_id": self._contract_id(chain_id, market_id, outcome_index),
            "live": True,
            "preflight": audit,
            "submission": "evm_rpc_eth_sendRawTransaction",
            "tx_hash": response,
            "dex_address": target,
            "chain_id": chain_id,
            "method": method,
            "outcome_index": outcome_index,
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Seer copy trading is unsupported because the documented public API does not expose account-activity mirroring as an order stream.",
        )

    def _post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.runtime.request_json("POST", self._url(path), json_body=dict(body), headers={})

    def _get(self, path: str, *, params: Mapping[str, Any]) -> Any:
        return self.runtime.request_json("GET", self._url(path), params=dict(params), headers={})

    def _get_market(self, chain_id: str, market_id: str) -> Mapping[str, Any]:
        payload = self._post("/get-market", {"chainId": int(chain_id), "id": market_id})
        row = payload.get("market") if isinstance(payload, Mapping) and isinstance(payload.get("market"), Mapping) else payload
        if not isinstance(row, Mapping):
            raise MarketConfigurationError(f"Seer market {market_id!r} was not found.")
        self._market_cache[self._event_id(chain_id, market_id)] = dict(row)
        return row

    def _event_from_row(self, row: Mapping[str, Any], event_id: str, chain_id: str, market_id: str) -> MarketEvent:
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=self._title(row, market_id),
            url=self._market_url(row, market_id),
            status=self._status(row),
            raw=dict(row),
        )

    def _validate_order(self, order: PaperOrderRequest) -> Tuple[str, str, int]:
        self.ensure_order_market(order)
        chain_id, market_id, outcome_index = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() not in self.live_order_sides:
            raise MarketConfigurationError("Seer order side must be BUY or SELL.")
        try:
            size = float(order.size)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Seer order size must be numeric.") from exc
        if not math.isfinite(size) or size <= 0:
            raise MarketConfigurationError("Seer order size must be positive and finite.")
        if order.limit_price is not None and self._probability(order.limit_price) is None:
            raise MarketConfigurationError("Seer order limit price must be between 0 and 1.")
        return chain_id, market_id, outcome_index

    @staticmethod
    def _market_rows(payload: Any) -> List[Mapping[str, Any]]:
        if isinstance(payload, Mapping):
            rows = payload.get("markets")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
            if isinstance(payload.get("data"), Mapping):
                return SeerAdapter._market_rows(payload["data"])
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, Mapping)]
        raise MarketConfigurationError("Seer markets-search returned an unsupported payload shape.")

    @staticmethod
    def _title(row: Mapping[str, Any], market_id: str) -> str:
        return str(row.get("marketName") or row.get("title") or market_id).strip()

    @staticmethod
    def _status(row: Mapping[str, Any]) -> str:
        value = row.get("marketStatus") or row.get("status") or row.get("state")
        return str(value).strip().lower() if value not in (None, "") else "open"

    @staticmethod
    def _market_url(row: Mapping[str, Any], market_id: str) -> str:
        value = row.get("url")
        if value not in (None, ""):
            return str(value)
        return f"https://app.seer.pm/market/{market_id}"

    @staticmethod
    def _chain_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text.isdigit() or int(text) <= 0:
            raise MarketConfigurationError("Seer market chainId must be a positive decimal integer.")
        return text

    @classmethod
    def _address(cls, value: Any, *, label: str = "address") -> str:
        text = str(value or "").strip()
        if not SEER_ADDRESS_RE.fullmatch(text):
            raise MarketConfigurationError(f"Seer {label} must be a 20-byte hex address.")
        return text

    @classmethod
    def _event_id(cls, chain_id: str, market_id: str) -> str:
        return f"{chain_id}:{market_id}"

    @classmethod
    def _contract_id(cls, chain_id: str, market_id: str, outcome_index: int) -> str:
        return f"{chain_id}:{market_id}:{int(outcome_index)}"

    @classmethod
    def _split_event_id(cls, event_id: Any) -> Tuple[str, str]:
        text = str(event_id or "").strip()
        parts = text.split(":", 1)
        if len(parts) != 2:
            raise MarketConfigurationError("Seer event id must be '<chain_id>:<market-address>'.")
        return cls._chain_id(parts[0]), cls._address(parts[1], label="market id")

    @classmethod
    def _split_contract_id(cls, contract_id: Any) -> Tuple[str, str, int]:
        text = str(contract_id or "").strip()
        parts = text.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            raise MarketConfigurationError("Seer contract id must be '<chain_id>:<market-address>:<outcome-index>'.")
        index = int(parts[2])
        if index < 0:
            raise MarketConfigurationError("Seer outcome index must be non-negative.")
        return cls._chain_id(parts[0]), cls._address(parts[1], label="market id"), index

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 < number < 1 else None

    @staticmethod
    def _history_timestamp_bound(value: Any, label: str) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Seer {label} must be a non-negative finite Unix timestamp.")
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Seer {label} must be a non-negative finite Unix timestamp.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError(f"Seer {label} must be a non-negative finite Unix timestamp.")
        return timestamp

    @staticmethod
    def _chart_timestamp(value: Any) -> float:
        if isinstance(value, bool):
            raise MarketConfigurationError("Seer market-chart timestamp must be finite and non-negative.")
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Seer market-chart timestamp must be finite and non-negative.") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise MarketConfigurationError("Seer market-chart timestamp must be finite and non-negative.")
        return timestamp

    @staticmethod
    def _chart_number(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise MarketConfigurationError("Seer market-chart prices must be finite non-negative numbers.")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @classmethod
    def _chart_prices(cls, point: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        token0_price = cls._chart_number(point.get("token0Price"))
        token1_price = cls._chart_number(point.get("token1Price"))
        if token0_price == 0 and token1_price == 0:
            # Seer's UI can derive from sqrtPrice only because it owns the
            # token metadata needed for decimal adjustment.  The normalized
            # adapter contract does not, so it must not fabricate that value.
            return None, None
        return token0_price, token1_price

    @classmethod
    def _pool_token_address(cls, value: Any, *, label: str) -> str:
        token = value.get("id") if isinstance(value, Mapping) else value
        return cls._address(token, label=label)

    def _url(self, path: str) -> str:
        allowed = {"/markets-search", "/get-market", "/market-chart"}
        if path not in allowed:
            raise MarketConfigurationError("Seer request path is not an approved official endpoint.")
        return f"{self.api_base_url}/.netlify/functions{path}"

    @property
    def _configured_rpc_url(self) -> str:
        configured = self.config.get("seer_rpc_url") or self.config.get("evm_rpc_url")
        if not configured:
            return ""
        value = str(configured).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Seer RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def trading_contract_addresses(self) -> Tuple[str, ...]:
        configured = self.config.get("seer_trading_contract_addresses")
        if configured in (None, ""):
            return ()
        values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
        addresses: List[str] = []
        for value in values:
            address = self._address(value, label="trading contract")
            if address.casefold() not in {item.casefold() for item in addresses}:
                addresses.append(address)
        return tuple(addresses)

    @staticmethod
    def _validate_signed_transaction(value: str) -> None:
        if (
            not re.fullmatch(r"0x[0-9a-fA-F]+", value)
            or len(value) % 2
            or len(value) < 2 + 64 * 2
            or len(value) > 2 + 1_000_000 * 2
        ):
            raise MarketConfigurationError(
                "Seer signed_transaction must be canonical 0x-prefixed hex between 64 bytes and 1 MB."
            )

    def _evm_rpc(self, url: str, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Seer RPC returned an invalid JSON-RPC payload.")
        if payload.get("error"):
            raise MarketHTTPError(f"Seer RPC error: {payload['error']}")
        if "result" not in payload:
            raise MarketHTTPError("Seer RPC response omitted result.")
        return payload["result"]
