from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketContract, MarketEvent, OrderBookSnapshot, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_ZETARIUM_RPC_URL = "https://bsc-dataseed.binance.org"
DEFAULT_ZETARIUM_PREDICTION_MARKET = "0xfc5fa5bb5f6a812600c303b4b83ee8dbdc021d99"
DEFAULT_ZETARIUM_CHAIN_ID = 56
ZETARIUM_REFERENCES = (
    "https://docs.zetarium.world/docs/products/prediction",
    "https://docs.zetarium.world/docs/overview/smart-contracts",
    "https://bscscan.com/address/0xfc5fa5bb5f6a812600c303b4b83ee8dbdc021d99",
)
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")

# Selectors from the reviewed, verified BSC PredictionMarket deployment. The
# adapter intentionally uses only the small read/write surface needed for
# market discovery, pool-share prices, and externally signed BUY intents.
SELECTORS = {
    "next_market_id": "406ef2ef",
    "markets": "b1283e77",
    "total_stakes": "26e5a7af",
    "min_bet": "9619367d",
    "stake_token": "51ed6a30",
    "paused": "5c975abb",
    "decimals": "313ce567",
    "approve": "095ea7b3",
    "place_bet": "da866c48",
}
STATUS_NAMES = {0: "pending", 1: "open", 2: "closed", 3: "resolved", 4: "canceled"}
OUTCOME_NAMES = ("YES", "NO")


class ZetariumWorldAdapter(MarketAdapter):
    """BSC PredictionMarket adapter for Zetarium World.

    Zetarium's published V2 design uses a binary/multi-outcome AMM and an
    on-chain PredictionMarket contract. Reads use JSON-RPC and derive prices
    from the contract's pari-mutuel stake pools. Paper orders produce unsigned
    ERC-20 approval and ``placeBet`` calls; live execution only forwards a
    complete, externally signed transaction after strict chain/target/calldata
    checks. The adapter never signs, approves, settles, or copies accounts.
    """

    metadata = get_market_metadata("zetarium_world")
    live_order_sides = ("BUY",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._event_cache: Dict[str, Dict[str, Any]] = {}
        self._decimals_cache: Dict[str, int] = {}

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("zetarium_rpc_url") or self.config.get("evm_rpc_url")
        value = str(configured or DEFAULT_ZETARIUM_RPC_URL).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Zetarium RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def prediction_market_address(self) -> str:
        value = self.config.get("zetarium_prediction_market_address") or DEFAULT_ZETARIUM_PREDICTION_MARKET
        return self._address(value, label="PredictionMarket address")

    @property
    def chain_id(self) -> int:
        value = self.config.get("zetarium_chain_id", DEFAULT_ZETARIUM_CHAIN_ID)
        if isinstance(value, bool):
            raise MarketConfigurationError("Zetarium chain ID must be a positive integer.")
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Zetarium chain ID must be a positive integer.") from exc
        if parsed <= 0:
            raise MarketConfigurationError("Zetarium chain ID must be a positive integer.")
        return parsed

    @property
    def live_transaction_targets(self) -> Tuple[str, ...]:
        configured = self.config.get("zetarium_live_transaction_targets")
        if configured in (None, ""):
            return ()
        values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
        targets: List[str] = []
        for value in values:
            target = self._address(value, label="live transaction target")
            if target.casefold() not in {item.casefold() for item in targets}:
                targets.append(target)
        return tuple(targets)

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "rpc_url": self.rpc_url,
                "prediction_market_address": self.prediction_market_address,
                "chain_id": self.chain_id,
                "network": "BNB Smart Chain mainnet",
                "references": list(ZETARIUM_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "deployment_source": "reviewed_verified_bscscan_deployment",
                "dynamic_discovery": not bool(self.config.get("zetarium_market_ids")),
                "allowlisted_live_transaction_target_count": len(self.live_transaction_targets),
                "wallet_transaction_required": True,
                "settlement_required": True,
                "orderbook_supported": False,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 100))
        needle = str(query or "").strip().lower()
        self._event_cache = {}
        events: List[MarketEvent] = []
        for market_id in self._market_ids():
            row = self._read_market(market_id)
            if not row["exists"]:
                continue
            event_id = self._event_id(market_id)
            title = f"Zetarium market {market_id}"
            status = str(row["status_name"])
            search_text = f"{event_id} {title} {status}".lower()
            if needle and needle not in search_text:
                continue
            event = MarketEvent(
                market_id=self.market_id,
                event_id=event_id,
                title=title,
                url="https://prediction.zetarium.world/",
                status=status,
                raw={"market": dict(row), "prediction_market_address": self.prediction_market_address},
            )
            self._event_cache[event_id] = {"event": event, "market": row}
            events.append(event)
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        market_id = self._split_event_id(event_id)
        canonical = self._event_id(market_id)
        bucket = self._event_bucket(canonical)
        row = bucket["market"]
        count = int(row["outcome_count"])
        if count < 2 or count > 16:
            raise MarketConfigurationError("Zetarium market outcome count is outside the supported range (2-16).")
        contracts: List[MarketContract] = []
        for outcome_id in range(count):
            outcome = OUTCOME_NAMES[outcome_id] if count == 2 else f"OUTCOME_{outcome_id}"
            contracts.append(
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"{canonical}:{outcome_id}",
                    event_id=canonical,
                    title=f"Zetarium market {market_id} - {outcome}",
                    outcome=outcome,
                    url="https://prediction.zetarium.world/",
                    status=str(row["status_name"]),
                    raw={"market": dict(row), "outcome_id": outcome_id},
                )
            )
        return contracts

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        market_id, outcome_id = self._split_contract_id(contract_id)
        row = self._market_row(market_id)
        count = int(row["outcome_count"])
        if outcome_id >= count:
            raise MarketConfigurationError("Zetarium outcome is not present in the market.")
        stakes = [self._total_stakes(market_id, index) for index in range(count)]
        total = sum(stakes)
        if total <= 0:
            raise MarketHTTPError("Zetarium market has no funded outcome pool.")
        probability = float(stakes[outcome_id]) / float(total)
        if not math.isfinite(probability) or probability < 0 or probability > 1:
            raise MarketHTTPError("Zetarium returned an invalid pool-share probability.")
        canonical = f"{self._event_id(market_id)}:{outcome_id}"
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=probability,
            bid=probability,
            ask=probability,
            midpoint=probability,
            source="zetarium_prediction_pool_share",
            raw={"market": dict(row), "stakes": stakes, "pool_total": total, "outcome_id": outcome_id},
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Zetarium exposes pari-mutuel outcome pools rather than a public CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        context = self._prepare_order(order)
        price = context["price"]
        amount_raw = context["amount_raw"]
        token = self._call_address(self.prediction_market_address, SELECTORS["stake_token"])
        approve_data = "0x" + SELECTORS["approve"] + self._address_arg(self.prediction_market_address) + self._uint_arg(amount_raw)
        place_data = self._place_bet_calldata(context["market_id"], context["outcome_id"], amount_raw)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=context["contract_id"],
            accepted=True,
            message=f"DRY RUN: would buy Zetarium outcome for {context['size']:g} token units at probability {price:.6f}",
            average_price=price,
            raw={
                "dry_run": True,
                "prediction_market_address": self.prediction_market_address,
                "market_id": context["market_id"],
                "outcome_id": context["outcome_id"],
                "amount": context["size"],
                "amount_raw": amount_raw,
                "amount_scale": context["amount_scale"],
                "minimum_bet_amount_raw": context["minimum_bet"],
                "stake_token": token,
                "unsigned_approval_call": {"to": token, "data": approve_data, "method": "approve(address,uint256)"},
                "unsigned_place_bet_call": {
                    "to": self.prediction_market_address,
                    "data": place_data,
                    "method": "placeBet(uint256,uint8,uint256)",
                },
                "wallet_transaction_required": True,
                "settlement_required": True,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        context = self._prepare_order(order)
        audit = self.preflight_live_order(order, feature_name="Zetarium live trading")
        if not self.config_bool("zetarium_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "Zetarium live trading requires zetarium_submit_signed_transactions=true after reviewing the signed transaction."
            )
        targets = self.live_transaction_targets
        if not targets:
            raise MarketConfigurationError(
                "Zetarium live orders require at least one explicitly reviewed zetarium_live_transaction_targets entry."
            )
        metadata = dict(order.metadata or {})
        signed = metadata.get("signed_transaction")
        decoded = self._decode_signed_transaction(signed)
        reviewed_chain = self._metadata_int(metadata, "chain_id", label="reviewed chain ID")
        if reviewed_chain != self.chain_id or decoded["chain_id"] != self.chain_id:
            raise MarketConfigurationError("Zetarium signed transaction targets a different chain than configured.")
        reviewed_target = self._address(metadata.get("transaction_to"), label="reviewed transaction target")
        if reviewed_target.casefold() != str(decoded["to"]).casefold():
            raise MarketConfigurationError("Zetarium signed transaction recipient does not match reviewed metadata.")
        if not any(str(decoded["to"]).casefold() == target.casefold() for target in targets):
            raise MarketConfigurationError("Zetarium signed transaction recipient is outside the reviewed target allow-list.")
        if str(decoded["to"]).casefold() != self.prediction_market_address.casefold():
            raise MarketConfigurationError("Zetarium signed transaction must target the configured PredictionMarket.")
        reviewed_value = self._metadata_int(metadata, "transaction_value", label="reviewed transaction value", minimum=0)
        if int(decoded["value"]) != 0 or reviewed_value != int(decoded["value"]):
            raise MarketConfigurationError("Zetarium placeBet transaction value must be zero.")
        reviewed_data = self._calldata(metadata.get("transaction_data"), label="reviewed transaction calldata")
        expected_data = self._place_bet_calldata(context["market_id"], context["outcome_id"], context["amount_raw"])
        if reviewed_data.casefold() != str(decoded["data"]).casefold() or reviewed_data.casefold() != expected_data.casefold():
            raise MarketConfigurationError("Zetarium signed transaction calldata does not match this order.")
        reviewed_market = self._metadata_int(metadata, "market_id", label="reviewed market ID", minimum=0)
        if reviewed_market != context["market_id"]:
            raise MarketConfigurationError("Zetarium signed transaction metadata targets a different market.")
        reviewed_outcome = self._metadata_int(metadata, "outcome_id", label="reviewed outcome ID", minimum=0)
        if reviewed_outcome != context["outcome_id"]:
            raise MarketConfigurationError("Zetarium signed transaction metadata targets a different outcome.")
        if str(metadata.get("side") or "").strip().upper() != str(order.side or "").upper():
            raise MarketConfigurationError("Zetarium signed transaction metadata uses a different order side.")
        self._validate_reviewed_number(metadata, "size", order.size, label="order size")
        self._validate_reviewed_limit_price(metadata, order.limit_price)
        response = self._rpc("eth_sendRawTransaction", [signed])
        if not isinstance(response, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", response):
            raise MarketHTTPError("Zetarium RPC did not return a transaction hash.")
        return {
            "live": True,
            "tx_hash": response,
            "audit": audit,
            "signed_transaction_submitted": True,
            "chain_id": decoded["chain_id"],
            "transaction_to": str(decoded["to"]),
            "transaction_value": decoded["value"],
            "calldata_selector": expected_data[:10],
            "market_id": context["market_id"],
            "outcome_id": context["outcome_id"],
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Zetarium does not publish an official account-activity mirror for copy trading.",
        )

    def _prepare_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        market_id, outcome_id = self._split_contract_id(order.contract_id)
        if str(order.side or "").strip().upper() != "BUY":
            raise MarketConfigurationError("Zetarium orders support BUY pool intents only.")
        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError("Zetarium order size must be positive.")
        price = None
        if order.limit_price is not None:
            price = self._finite_float(order.limit_price, "limit price")
            if price <= 0 or price > 1:
                raise MarketConfigurationError("Zetarium limit price must be greater than 0 and at most 1.")
        row = self._market_row(market_id)
        if row["status_name"] != "open":
            raise MarketConfigurationError(f"Zetarium market {market_id} is {row['status_name']}; orders require an open market.")
        if outcome_id >= int(row["outcome_count"]):
            raise MarketConfigurationError("Zetarium outcome is not present in the market.")
        amount_scale = float(10 ** self._token_decimals())
        amount_raw = int(round(size * amount_scale))
        if amount_raw <= 0:
            raise MarketConfigurationError("Zetarium order size is below token precision.")
        minimum_bet = self._call_uint(self.prediction_market_address, SELECTORS["min_bet"])
        if amount_raw < minimum_bet:
            raise MarketConfigurationError(f"Zetarium amount {amount_raw} is below contract minimum {minimum_bet}.")
        canonical = f"{self._event_id(market_id)}:{outcome_id}"
        if price is None:
            price = self.get_price(canonical).last
        if price is None:
            raise MarketHTTPError("Zetarium did not return a price for the requested outcome.")
        return {
            "market_id": market_id,
            "outcome_id": outcome_id,
            "contract_id": canonical,
            "size": size,
            "price": float(price),
            "amount_scale": amount_scale,
            "amount_raw": amount_raw,
            "minimum_bet": minimum_bet,
        }

    def _market_ids(self) -> List[int]:
        configured = self.config.get("zetarium_market_ids")
        if configured not in (None, ""):
            values = configured if isinstance(configured, (list, tuple, set)) else str(configured).split(",")
            ids: List[int] = []
            for value in values:
                market_id = self._uint(value, "market ID")
                if market_id not in ids:
                    ids.append(market_id)
            return ids
        next_id = self._call_uint(self.prediction_market_address, SELECTORS["next_market_id"])
        max_scan = self._uint(self.config.get("zetarium_max_market_scan", 500), "maximum market scan")
        if max_scan < 1 or max_scan > 5000:
            raise MarketConfigurationError("Zetarium maximum market scan must be between 1 and 5000.")
        return list(range(1, min(next_id, max_scan + 1)))

    def _event_bucket(self, event_id: str) -> Dict[str, Any]:
        market_id = self._split_event_id(event_id)
        canonical = self._event_id(market_id)
        cached = self._event_cache.get(canonical)
        if cached is not None:
            return cached
        row = self._read_market(market_id)
        if not row["exists"]:
            raise MarketConfigurationError("Zetarium market does not exist.")
        event = MarketEvent(
            market_id=self.market_id,
            event_id=canonical,
            title=f"Zetarium market {market_id}",
            url="https://prediction.zetarium.world/",
            status=str(row["status_name"]),
            raw={"market": dict(row)},
        )
        bucket = {"event": event, "market": row}
        self._event_cache[canonical] = bucket
        return bucket

    def _market_row(self, market_id: int) -> Dict[str, Any]:
        canonical = self._event_id(market_id)
        bucket = self._event_cache.get(canonical)
        return bucket["market"] if bucket else self._read_market(market_id)

    def _read_market(self, market_id: int) -> Dict[str, Any]:
        decoded = self._decode(
            self._call(self.prediction_market_address, SELECTORS["markets"] + self._uint_arg(market_id)),
            ("uint256", "uint8", "uint8", "uint8", "uint64", "uint64", "uint16", "address", "uint256", "bool"),
        )
        return {
            "id": int(decoded[0]),
            "status": int(decoded[1]),
            "status_name": STATUS_NAMES.get(int(decoded[1]), f"unknown:{int(decoded[1])}"),
            "outcome_count": int(decoded[2]),
            "winning_outcome": int(decoded[3]),
            "start_time": int(decoded[4]),
            "end_time": int(decoded[5]),
            "fee_bps": int(decoded[6]),
            "creator": self._address(decoded[7]),
            "total_pool": int(decoded[8]),
            "exists": bool(decoded[9]),
        }

    def _total_stakes(self, market_id: int, outcome_id: int) -> int:
        return self._call_uint(
            self.prediction_market_address,
            SELECTORS["total_stakes"] + self._uint_arg(market_id) + self._uint_arg(outcome_id),
        )

    def _token_decimals(self) -> int:
        token = self._call_address(self.prediction_market_address, SELECTORS["stake_token"])
        key = token.lower()
        if key not in self._decimals_cache:
            value = self._call_uint(token, SELECTORS["decimals"])
            if value < 0 or value > 36:
                raise MarketConfigurationError("Zetarium stake-token decimals are outside the supported range.")
            self._decimals_cache[key] = value
        return self._decimals_cache[key]

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Zetarium RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Zetarium RPC error: {payload['error']}")
        return payload.get("result")

    def _call(self, address: str, data: str) -> str:
        result = self._rpc("eth_call", [{"to": self._address(address), "data": "0x" + data}, "latest"])
        if not isinstance(result, str) or not HEX_RE.fullmatch(result) or len(result) < 2:
            raise MarketHTTPError("Zetarium eth_call did not return hex data.")
        return result

    def _call_uint(self, address: str, data: str) -> int:
        values = self._decode(self._call(address, data), ("uint256",))
        return self._uint(values[0], "RPC uint")

    def _call_address(self, address: str, data: str) -> str:
        values = self._decode(self._call(address, data), ("address",))
        return self._address(values[0], label="RPC address")

    @classmethod
    def _decode_signed_transaction(cls, value: Any) -> Dict[str, Any]:
        if not isinstance(value, str) or len(value) < 2 + 65 * 2 or len(value) % 2 or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
            raise MarketConfigurationError("Zetarium live orders require a canonical 0x-prefixed signed transaction.")
        try:
            from eth_account._utils.legacy_transactions import Transaction
            from eth_account.typed_transactions import TypedTransaction
            from hexbytes import HexBytes
            raw = bytes.fromhex(value[2:])
            if raw[0] <= 0x7F:
                tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
                chain_id = int(tx["chainId"])
                tx_type = int(tx.get("type", raw[0]))
            else:
                tx = Transaction.from_bytes(raw).as_dict()
                signature_v = int(tx["v"])
                if signature_v < 35:
                    raise ValueError("legacy transaction is not chain protected")
                chain_id = (signature_v - 35) // 2
                tx_type = 0
            target = bytes(tx["to"])
            if len(target) != 20:
                raise ValueError("contract creation transactions are unsupported")
            value_int = int(tx.get("value", 0))
            if chain_id <= 0 or value_int < 0:
                raise ValueError("invalid chain ID or value")
            return {"chain_id": chain_id, "to": "0x" + target.hex(), "data": "0x" + bytes(tx.get("data", b"")).hex(), "value": value_int, "type": tx_type}
        except Exception as exc:
            raise MarketConfigurationError("Zetarium signed_transaction could not be decoded as a chain-protected EVM transaction.") from exc

    @staticmethod
    def _decode(value: str, types: Tuple[str, ...]) -> Tuple[Any, ...]:
        try:
            from eth_abi import decode

            return tuple(decode(list(types), bytes.fromhex(value[2:])))
        except (ImportError, ValueError, TypeError, OverflowError) as exc:
            raise MarketConfigurationError("Zetarium RPC returned data that did not match the documented ABI.") from exc

    @classmethod
    def _address(cls, value: Any, *, label: str = "address") -> str:
        text = str(value or "").strip()
        if not ADDRESS_RE.fullmatch(text):
            raise MarketConfigurationError(f"Zetarium {label} must be a 20-byte hex address.")
        return text

    @classmethod
    def _uint(cls, value: Any, label: str) -> int:
        try:
            number = int(str(value).strip(), 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Zetarium {label} must be a non-negative integer.") from exc
        if number < 0:
            raise MarketConfigurationError(f"Zetarium {label} must be a non-negative integer.")
        return number

    @classmethod
    def _uint_arg(cls, value: Any) -> str:
        return f"{cls._uint(value, 'ABI argument'):064x}"

    @classmethod
    def _address_arg(cls, value: Any) -> str:
        return "0" * 24 + cls._address(value)[2:].lower()

    def _event_id(self, market_id: int) -> str:
        return f"{self.prediction_market_address.lower()}:{int(market_id)}"

    def _split_event_id(self, value: Any) -> int:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2 or parts[0].casefold() != self.prediction_market_address.casefold():
            raise MarketConfigurationError("Zetarium event id must be '<prediction-market-address>:<market-id>'.")
        return self._uint(parts[1], "market ID")

    def _split_contract_id(self, value: Any) -> Tuple[int, int]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 3 or parts[0].casefold() != self.prediction_market_address.casefold():
            raise MarketConfigurationError("Zetarium contract id must be '<prediction-market-address>:<market-id>:<outcome-id>'.")
        return self._uint(parts[1], "market ID"), self._uint(parts[2], "outcome ID")

    def _place_bet_calldata(self, market_id: int, outcome_id: int, amount_raw: int) -> str:
        return "0x" + SELECTORS["place_bet"] + self._uint_arg(market_id) + self._uint_arg(outcome_id) + self._uint_arg(amount_raw)

    @staticmethod
    def _metadata_int(metadata: Mapping[str, Any], key: str, *, label: str, minimum: int = 1) -> int:
        value = metadata.get(key)
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Zetarium live orders require an integer {label}.")
        try:
            parsed = int(str(value).strip(), 0)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Zetarium live orders require an integer {label}.") from exc
        if parsed < minimum:
            raise MarketConfigurationError(f"Zetarium live orders require {label} to be at least {minimum}.")
        return parsed

    @staticmethod
    def _calldata(value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]+", text) or len(text) % 2 or len(text) < 10:
            raise MarketConfigurationError(f"Zetarium live orders require hexadecimal {label} with a 4-byte selector.")
        return "0x" + text[2:].lower()

    @classmethod
    def _validate_reviewed_number(cls, metadata: Mapping[str, Any], key: str, expected: Any, *, label: str) -> None:
        reviewed = cls._finite_float(metadata.get(key), f"reviewed {label}")
        requested = cls._finite_float(expected, label)
        if reviewed != requested:
            raise MarketConfigurationError(f"Zetarium signed transaction metadata uses a different {label}.")

    @classmethod
    def _validate_reviewed_limit_price(cls, metadata: Mapping[str, Any], expected: Any) -> None:
        reviewed = metadata.get("limit_price")
        if expected is None:
            if reviewed is not None:
                raise MarketConfigurationError("Zetarium signed transaction metadata uses a different limit price.")
            return
        cls._validate_reviewed_number(metadata, "limit_price", expected, label="limit price")
