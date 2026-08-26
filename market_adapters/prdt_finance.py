from __future__ import annotations

import math
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


PRDT_REFERENCES = (
    "https://prdt.finance/en",
    "https://prdt-finance.gitbook.io/prdt-finance-gitbook/smart-contracts",
    "https://github.com/PRDTfinance/contract",
)
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_POSITION_NAMES = ("BULL", "BEAR")

# Selectors from PRDT's published Prediction/PredictionFactory Solidity source.
_SELECTORS = {
    "current_epoch": "76671808",
    "interval_seconds": "7d1cd04f",
    "min_bet_amount": "fa968eea",
    "bet_token": "78691f16",
    "oracle_info": "7755244e",
    "round": "8c65c81f",
    "timestamps": "8bc33af3",
}


class PRDTFinanceAdapter(MarketAdapter):
    """Configured read-only/paper adapter for PRDT Prediction contracts.

    PRDT publishes the Prediction and PredictionFactory Solidity contracts, but
    its current public deployment list does not provide a stable factory/indexer
    inventory that can be selected safely by this application.  The adapter
    therefore requires an explicit list of deployed Prediction contract
    addresses.  It reads the documented round/timestamp mappings through EVM
    JSON-RPC and derives pool-share probabilities; it never guesses deployments,
    scrapes the consumer app, infers CLOB depth, or submits wallet transactions.
    """

    metadata = get_market_metadata("prdt_finance")
    live_order_sides = ("BUY",)

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        runtime=None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        super().__init__(config, runtime=runtime)
        self._clock = clock or time.time
        self._event_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("prdt_rpc_url") or self.config.get("evm_rpc_url")
        value = str(configured or "").strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError(
                "PRDT RPC URL must be an absolute http(s) URL without query or fragment."
            )
        return value

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        try:
            specs = self._market_specs(allow_empty=True)
            config_error = ""
        except MarketConfigurationError as exc:
            specs = []
            config_error = str(exc)
        rpc_configured = bool(self.config.get("prdt_rpc_url") or self.config.get("evm_rpc_url"))
        health.update(
            {
                "rpc_url_configured": rpc_configured,
                "configured_market_count": len(specs),
                "configured_prediction_contracts": [spec["address"] for spec in specs],
                "dynamic_discovery": False,
                "configuration_required": True,
                "configuration_error": config_error,
                "references": list(PRDT_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "contract_model": "Prediction / PredictionFactory",
                "pool_probability_model": "bullAmount/(bullAmount+bearAmount)",
                "orderbook_supported": False,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "wallet_transaction_required": True,
                "settlement_required": True,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        specs = self._market_specs()
        desired = max(1, min(int(limit or 50), 100))
        needle = str(query or "").strip().lower()
        self._event_cache = {}
        events: List[MarketEvent] = []
        for spec in specs:
            row = self._read_prediction(spec)
            event_id = self._event_id(spec["address"], row["epoch"])
            title = str(spec.get("title") or f"PRDT {spec.get('asset') or 'prediction'} epoch {row['epoch']}")
            status = self._status(row)
            search_text = f"{event_id} {title} {spec.get('asset', '')} {status}".lower()
            if needle and needle not in search_text:
                continue
            event = MarketEvent(
                market_id=self.market_id,
                event_id=event_id,
                title=title,
                url=str(spec.get("url") or "https://prdt.finance/en"),
                status=status,
                raw={"inventory": dict(spec), "prediction": dict(row)},
            )
            self._event_cache[event_id] = {"event": event, "spec": dict(spec), "prediction": row}
            events.append(event)
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        canonical, position = self._split_contract_id(f"{event_id}:BULL")
        if position != "BULL":
            raise MarketConfigurationError("PRDT event ids must be used to list contracts.")
        bucket = self._event_bucket(canonical)
        event = bucket["event"]
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=f"{canonical}:{outcome}",
                event_id=canonical,
                title=f"{event.title} - {outcome}",
                outcome=outcome,
                url=event.url,
                status=event.status,
                raw={"prediction": dict(bucket["prediction"]), "position": outcome},
            )
            for outcome in _POSITION_NAMES
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        canonical, position = self._split_contract_id(contract_id)
        bucket = self._event_bucket(canonical)
        prediction = bucket["prediction"]
        bull = float(prediction["round"]["bull_amount"])
        bear = float(prediction["round"]["bear_amount"])
        total = bull + bear
        if not math.isfinite(total) or total <= 0:
            raise MarketHTTPError("PRDT Prediction round has no funded bull/bear pool.")
        probability = bull / total if position == "BULL" else bear / total
        if not math.isfinite(probability) or probability < 0 or probability > 1:
            raise MarketHTTPError("PRDT Prediction round returned an invalid pool probability.")
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=f"{canonical}:{position}",
            last=probability,
            bid=probability,
            ask=probability,
            midpoint=probability,
            source="prdt_prediction_pool_share",
            raw={"prediction": dict(prediction), "position": position, "pool_total": total},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "PRDT Prediction contracts expose pooled bull/bear amounts rather than a public CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        canonical, position = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() != "BUY":
            raise MarketConfigurationError("PRDT paper orders support BUY position intents only.")
        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError("PRDT order size must be positive.")
        limit_price = None
        if order.limit_price is not None:
            limit_price = self._finite_float(order.limit_price, "limit price")
            if limit_price <= 0 or limit_price > 1:
                raise MarketConfigurationError("PRDT limit price must be greater than 0 and at most 1.")
        bucket = self._event_bucket(canonical)
        prediction = bucket["prediction"]
        status = self._status(prediction)
        if status != "open":
            raise MarketConfigurationError(
                f"PRDT round {prediction['epoch']} is {status}; paper intents are accepted only before the lock timestamp."
            )
        if limit_price is None:
            limit_price = self.get_price(f"{canonical}:{position}").last
        spec = bucket["spec"]
        amount_scale = self._amount_scale()
        amount_raw = int(round(size * amount_scale))
        if amount_raw <= 0:
            raise MarketConfigurationError("PRDT order size is below the configured raw-unit precision.")
        minimum_bet_amount = int(prediction["min_bet_amount"])
        if amount_raw < minimum_bet_amount:
            raise MarketConfigurationError(
                f"PRDT order amount {amount_raw} is below the contract minimum {minimum_bet_amount}."
            )
        factory_address = spec.get("factory_address")
        factory_index = spec.get("factory_index")
        unsigned_call = None
        if factory_address is not None and factory_index is not None:
            selector = "31dfce9e" if position == "BULL" else "0e705fa7"
            unsigned_call = {
                "to": self._address(factory_address, label="factory address"),
                "data": "0x" + selector + self._uint_arg(factory_index) + self._uint_arg(prediction["epoch"]) + self._uint_arg(amount_raw),
                "method": "betBull(uint256,uint256,uint256)" if position == "BULL" else "betBear(uint256,uint256,uint256)",
                "approval_required": True,
            }
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=f"{canonical}:{position}",
            accepted=True,
            message=f"DRY RUN: would buy PRDT {position} for {size:g} token units at probability {float(limit_price):.6f}",
            filled_size=0.0,
            average_price=limit_price,
            raw={
                "dry_run": True,
                "prediction_contract": spec["address"],
                "epoch": prediction["epoch"],
                "position": position,
                "amount": size,
                "amount_raw": amount_raw,
                "amount_scale": amount_scale,
                "minimum_bet_amount_raw": minimum_bet_amount,
                "bet_token": prediction["bet_token"],
                "unsigned_factory_call": unsigned_call,
                "wallet_transaction_required": True,
                "settlement_required": True,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "PRDT live execution requires an approved factory deployment, ERC-20 approval, wallet signing, and settlement evidence; this adapter is read-only/paper only.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "PRDT does not publish an official account-activity mirroring API for copy trading.",
        )

    def _read_prediction(self, spec: Mapping[str, Any]) -> Dict[str, Any]:
        address = str(spec["address"])
        epoch = self._call_uint(address, _SELECTORS["current_epoch"])
        row = {
            "epoch": epoch,
            "interval_seconds": self._call_uint(address, _SELECTORS["interval_seconds"]),
            "min_bet_amount": self._call_uint(address, _SELECTORS["min_bet_amount"]),
            "bet_token": self._call_address(address, _SELECTORS["bet_token"]),
            "oracle": self._call_address(address, _SELECTORS["oracle_info"]),
        }
        round_values = self._decode(
            self._call(address, _SELECTORS["round"] + self._uint_arg(epoch)),
            ("bool", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256", "int256", "int256"),
        )
        timestamp_values = self._decode(
            self._call(address, _SELECTORS["timestamps"] + self._uint_arg(epoch)),
            ("uint32", "uint32", "uint32"),
        )
        row["round"] = {
            "oracle_called": bool(round_values[0]),
            "bull_amount": int(round_values[1]),
            "bear_amount": int(round_values[2]),
            "reward_base_amount": int(round_values[3]),
            "reward_amount": int(round_values[4]),
            "treasury_amount": int(round_values[5]),
            "bull_bonus_amount": int(round_values[6]),
            "bear_bonus_amount": int(round_values[7]),
            "lock_price": int(round_values[8]),
            "close_price": int(round_values[9]),
        }
        row["timestamps"] = {
            "start": int(timestamp_values[0]),
            "lock": int(timestamp_values[1]),
            "close": int(timestamp_values[2]),
        }
        return row

    def _event_bucket(self, event_id: str) -> Dict[str, Any]:
        address, epoch = self._split_event_id(event_id)
        canonical = self._event_id(address, epoch)
        cached = self._event_cache.get(canonical)
        if cached is not None:
            return cached
        spec = self._spec_for_address(address)
        prediction = self._read_prediction(spec)
        if int(prediction["epoch"]) != epoch:
            raise MarketConfigurationError(
                f"PRDT event epoch {epoch} is not the current configured epoch {prediction['epoch']}."
            )
        event = MarketEvent(
            market_id=self.market_id,
            event_id=canonical,
            title=str(spec.get("title") or f"PRDT {spec.get('asset') or 'prediction'} epoch {epoch}"),
            url=str(spec.get("url") or "https://prdt.finance/en"),
            status=self._status(prediction),
            raw={"inventory": dict(spec), "prediction": dict(prediction)},
        )
        bucket = {"event": event, "spec": dict(spec), "prediction": prediction}
        self._event_cache[canonical] = bucket
        return bucket

    def _spec_for_address(self, address: str) -> Dict[str, Any]:
        normalized = self._address(address, label="prediction address").lower()
        for spec in self._market_specs():
            if spec["address"].lower() == normalized:
                return spec
        raise MarketConfigurationError(f"PRDT prediction contract is not in the explicit configured inventory: {address}")

    def _market_specs(self, *, allow_empty: bool = False) -> List[Dict[str, Any]]:
        configured: Any = self.config.get("prdt_prediction_contracts")
        if configured in (None, ""):
            if allow_empty:
                return []
            raise MarketConfigurationError(
                "PRDT requires prdt_prediction_contracts with deployed Prediction addresses; the adapter does not guess current deployments."
            )
        if isinstance(configured, Mapping):
            if "address" in configured or "prediction_address" in configured:
                configured = [configured]
            else:
                configured = [dict(value, title=key) if isinstance(value, Mapping) else value for key, value in configured.items()]
        if not isinstance(configured, (list, tuple)):
            raise MarketConfigurationError("PRDT prediction contracts must be a list or mapping of objects.")
        specs: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in configured:
            if not isinstance(item, Mapping):
                raise MarketConfigurationError("PRDT prediction contracts must contain objects.")
            address = self._address(item.get("address", item.get("prediction_address")), label="prediction address")
            key = address.lower()
            if key in seen:
                continue
            seen.add(key)
            spec = dict(item)
            spec["address"] = address
            if item.get("factory_address") not in (None, ""):
                spec["factory_address"] = self._address(item["factory_address"], label="factory address")
            if item.get("factory_index") not in (None, ""):
                spec["factory_index"] = self._uint(item["factory_index"], "factory index")
            if item.get("title") not in (None, ""):
                title = str(item["title"]).strip()
                if not title or len(title) > 240:
                    raise MarketConfigurationError("PRDT contract titles must be 1-240 characters.")
                spec["title"] = title
            if item.get("asset") not in (None, ""):
                spec["asset"] = str(item["asset"]).strip()[:80]
            specs.append(spec)
        if not specs and not allow_empty:
            raise MarketConfigurationError("PRDT prediction contract inventory cannot be empty.")
        return specs

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("PRDT RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"PRDT RPC error: {payload['error']}")
        return payload.get("result")

    def _call(self, address: str, data: str) -> str:
        result = self._rpc("eth_call", [{"to": self._address(address), "data": "0x" + data}, "latest"])
        if not isinstance(result, str) or not _HEX_RE.fullmatch(result):
            raise MarketHTTPError("PRDT eth_call did not return hex data.")
        return result

    def _call_uint(self, address: str, selector: str) -> int:
        values = self._decode(self._call(address, selector), ("uint256",))
        return self._uint(values[0], "RPC uint")

    def _call_address(self, address: str, selector: str) -> str:
        values = self._decode(self._call(address, selector), ("address",))
        return self._address(values[0], label="RPC address")

    def _amount_scale(self) -> float:
        value = self.config.get("prdt_amount_scale", 1.0)
        scale = self._finite_float(value, "amount scale")
        if scale <= 0:
            raise MarketConfigurationError("PRDT amount scale must be positive.")
        return scale

    def _status(self, row: Mapping[str, Any]) -> str:
        if bool(row["round"]["oracle_called"]):
            return "settled"
        timestamps = row["timestamps"]
        now = int(self._clock())
        if now < int(timestamps["lock"]):
            return "open"
        if now < int(timestamps["close"]):
            return "locked"
        return "awaiting_oracle"

    @classmethod
    def _event_id(cls, address: str, epoch: int) -> str:
        return f"{cls._address(address).lower()}:{int(epoch)}"

    @classmethod
    def _split_event_id(cls, value: Any) -> Tuple[str, int]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2 or not parts[1].isdigit():
            raise MarketConfigurationError("PRDT event id must be '<prediction-address>:<epoch>'.")
        epoch = int(parts[1])
        if epoch < 0:
            raise MarketConfigurationError("PRDT epoch must be non-negative.")
        return cls._address(parts[0], label="prediction address").lower(), epoch

    @classmethod
    def _split_contract_id(cls, value: Any) -> Tuple[str, str]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 3 or parts[2].upper() not in _POSITION_NAMES:
            raise MarketConfigurationError("PRDT contract id must be '<prediction-address>:<epoch>:BULL' or ':BEAR'.")
        address, epoch = cls._split_event_id(":".join(parts[:2]))
        return f"{address}:{epoch}", parts[2].upper()

    @classmethod
    def _address(cls, value: Any, *, label: str = "address") -> str:
        text = str(value or "").strip()
        if not _ADDRESS_RE.fullmatch(text):
            raise MarketConfigurationError(f"PRDT {label} must be a 20-byte hex address.")
        return text

    @classmethod
    def _uint(cls, value: Any, label: str) -> int:
        try:
            if isinstance(value, str) and value.lower().startswith("0x"):
                number = int(value, 16)
            else:
                number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"PRDT {label} must be a non-negative integer.") from exc
        if number < 0:
            raise MarketConfigurationError(f"PRDT {label} must be a non-negative integer.")
        return number

    @classmethod
    def _uint_arg(cls, value: Any) -> str:
        return f"{cls._uint(value, 'ABI argument'):064x}"

    @staticmethod
    def _decode(value: str, types: Tuple[str, ...]) -> Tuple[Any, ...]:
        try:
            from eth_abi import decode

            return tuple(decode(list(types), bytes.fromhex(value[2:])))
        except (ImportError, ValueError, TypeError, OverflowError) as exc:
            raise MarketConfigurationError("PRDT RPC returned data that did not match the documented ABI.") from exc
