from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_FRENZY_RPC_URL = "https://mainnet.base.org"
DEFAULT_FRENZY_CHAIN_ID = 8453
DEFAULT_FRENZY_CONTRACT = "0xf116E1BC30D50b8769945Bc48Bd2c1BCdA3ff445"
DEFAULT_FRENZY_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FRENZY_CONTRACTS = {
    8453: DEFAULT_FRENZY_CONTRACT,
    84532: "0x6195f1d17BcE754bd3ebd670070ae05307a464Ba",
}
FRENZY_USDC_CONTRACTS = {
    8453: DEFAULT_FRENZY_USDC,
    84532: "0xD826235Fb2b20Dc0EAa37AAb84C7d6D58e49c7f5",
}
FRENZY_REFERENCES = (
    "https://frenzy.finance/docs",
    "https://frenzy.finance/docs/smart-contracts",
    "https://basescan.org/address/0xf116E1BC30D50b8769945Bc48Bd2c1BCdA3ff445#code",
)

# keccak256("BetSettled(bytes32,address,bytes32,uint64,uint256,uint256,bool)").
_BET_SETTLED_TOPIC = "0x8d80dcfb835e27822f1d1a72e3f508bda35ef2bb31371a566c6dce20963761c7"
_ADDRESS_RE = r"0x[0-9a-fA-F]{40}"
_BYTES32_RE = r"0x[0-9a-fA-F]{64}"
_ZERO_ADDRESS = "0x" + "0" * 40


class FrenzyFinanceAdapter(MarketAdapter):
    """Contract-backed Frenzy Finance discovery and paper-intent adapter.

    Frenzy's official protocol uses short-lived price-range cells and an
    oracle-signed ``BetIntent``.  The chain stores settled bets, but the
    active grid/quote and oracle acknowledgement are produced off-chain.
    This adapter therefore supports explicitly configured grid specs, reads
    settlement history from the official Base contract, and emits a safe
    EIP-712 intent preview.  It never signs, submits, or fabricates the
    oracle acknowledgement required for a live bet.
    """

    metadata = get_market_metadata("frenzy_finance")
    live_order_sides = ("BUY",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._event_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("frenzy_rpc_url") or self.config.get("evm_rpc_url")
        value = str(configured or DEFAULT_FRENZY_RPC_URL).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Frenzy RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def chain_id(self) -> int:
        value = self.config.get("frenzy_chain_id", DEFAULT_FRENZY_CHAIN_ID)
        try:
            chain_id = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Frenzy chain id must be an integer.") from exc
        if chain_id not in FRENZY_CONTRACTS:
            raise MarketConfigurationError("Frenzy chain id must be Base (8453) or Base Sepolia (84532).")
        return chain_id

    @property
    def contract_address(self) -> str:
        configured = self.config.get("frenzy_contract_address")
        value = str(configured or FRENZY_CONTRACTS[self.chain_id]).strip()
        return self._address(value, label="Frenzy contract")

    @property
    def usdc_address(self) -> str:
        configured = self.config.get("frenzy_usdc_address")
        value = str(configured or FRENZY_USDC_CONTRACTS[self.chain_id]).strip()
        return self._address(value, label="Frenzy USDC contract")

    @property
    def amount_scale(self) -> int:
        value = self.config.get("frenzy_amount_scale", 1_000_000)
        try:
            scale = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Frenzy amount scale must be a positive integer.") from exc
        if scale <= 0:
            raise MarketConfigurationError("Frenzy amount scale must be a positive integer.")
        return scale

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "rpc_url": self.rpc_url,
                "chain_id": self.chain_id,
                "contract_address": self.contract_address,
                "usdc_address": self.usdc_address,
                "references": list(FRENZY_REFERENCES),
                "onchain_reading": True,
                "contract_model": "BetIntent / BetSettled",
                "configured_market_count": len(self._market_specs()),
                "settlement_history_requires_block_range": True,
                "oracle_ack_required": True,
                "wallet_transaction_required": True,
                "live_trading_supported": False,
                "live_trading_enabled": False,
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 200))
        events: Dict[str, Dict[str, Any]] = {}
        for row in self._market_specs():
            event = self._event_from_spec(row)
            events[event.event_id] = {"event": event, "spec": row, "settlements": []}

        for settlement in self._settlement_logs():
            event_id = self._event_id(settlement["market_id"], settlement["interval_end"])
            bucket = events.setdefault(
                event_id,
                {
                    "event": self._event_from_settlement(settlement),
                    "spec": {},
                    "settlements": [],
                },
            )
            bucket["settlements"].append(settlement)
            if not bucket["spec"]:
                bucket["event"] = self._event_from_settlement(settlement)

        needle = str(query or "").strip().lower()
        selected: List[MarketEvent] = []
        self._event_cache = {}
        for event_id, bucket in events.items():
            event = bucket["event"]
            search_text = f"{event.event_id} {event.title} {event.status}".lower()
            if needle and needle not in search_text:
                continue
            raw = dict(event.raw)
            raw["spec"] = dict(bucket["spec"])
            raw["settlements"] = [dict(item) for item in bucket["settlements"]]
            event = MarketEvent(
                market_id=event.market_id,
                event_id=event.event_id,
                title=event.title,
                url=event.url,
                status=event.status,
                raw=raw,
            )
            self._event_cache[event_id] = {
                "event": event,
                "spec": dict(bucket["spec"]),
                "settlements": [dict(item) for item in bucket["settlements"]],
            }
            selected.append(event)
            if len(selected) >= desired:
                break
        return selected

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        canonical = self._canonical_event_id(event_id)
        bucket = self._event_bucket(canonical)
        spec = bucket["spec"]
        contracts = spec.get("contracts") if isinstance(spec, Mapping) else None
        if isinstance(contracts, list) and contracts:
            result: List[MarketContract] = []
            event = bucket["event"]
            for index, row in enumerate(contracts):
                if not isinstance(row, Mapping):
                    raise MarketConfigurationError("Frenzy market contracts must be objects.")
                contract_id = self._contract_id(canonical, index)
                outcome = self._range_label(row, index)
                result.append(
                    MarketContract(
                        market_id=self.market_id,
                        contract_id=contract_id,
                        event_id=canonical,
                        title=f"{event.title} - {outcome}",
                        outcome=outcome,
                        url=event.url,
                        status=event.status,
                        raw={"event": dict(event.raw), "contract": dict(row), "index": index},
                    )
                )
            return result

        settlements = bucket.get("settlements", [])
        if not settlements:
            raise MarketConfigurationError(
                "Frenzy event has no configured price-range contracts; provide frenzy_market_specs with contracts."
            )
        event = bucket["event"]
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=self._contract_id(canonical, 0),
                event_id=canonical,
                title=f"{event.title} - settled bets",
                outcome="SETTLED",
                url=event.url,
                status="settled",
                raw={"event": dict(event.raw), "settlements": [dict(item) for item in settlements]},
            )
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        event_id, index = self._split_contract_id(contract_id)
        bucket = self._event_bucket(event_id)
        spec = bucket["spec"]
        contracts = spec.get("contracts") if isinstance(spec, Mapping) else None
        if isinstance(contracts, list) and 0 <= index < len(contracts):
            row = contracts[index]
            price = self._probability(row.get("price", row.get("probability")))
            if price is None and row.get("multiplier") not in (None, ""):
                multiplier = self._positive_float(row.get("multiplier"), "Frenzy multiplier")
                price = 1.0 / multiplier
            if price is None:
                raise MarketHTTPError("Frenzy configured contract does not expose a bounded quote/probability.")
            return PriceSnapshot(
                market_id=self.market_id,
                contract_id=self._contract_id(event_id, index),
                last=price,
                bid=price,
                ask=price,
                midpoint=price,
                source="frenzy_configured_grid",
                raw={"event": dict(bucket["event"].raw), "contract": dict(row), "index": index},
            )

        settlements = bucket.get("settlements", [])
        if not settlements or index != 0:
            raise MarketHTTPError(
                "Frenzy settlement logs are historical only; configure a grid quote before requesting a live price."
            )
        won = [bool(item["won"]) for item in settlements]
        realized = sum(1 for item in won if item) / len(won)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=self._contract_id(event_id, index),
            last=realized,
            midpoint=realized,
            source="frenzy_settlement_history",
            raw={"event": dict(bucket["event"].raw), "settlements": [dict(item) for item in settlements]},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Frenzy uses a signed price-range grid rather than a public CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        event_id, index = self._split_contract_id(order.contract_id)
        bucket = self._event_bucket(event_id)
        spec = bucket["spec"]
        contracts = spec.get("contracts") if isinstance(spec, Mapping) else None
        if not isinstance(contracts, list) or not 0 <= index < len(contracts):
            raise MarketConfigurationError(
                "Frenzy paper orders require a configured price-range contract and cannot target settlement history."
            )
        side = str(order.side or "").upper()
        if side != "BUY":
            raise MarketConfigurationError("Frenzy paper orders support BUY intents only.")
        size = self._finite_positive(order.size, "order size")
        row = contracts[index]
        price_low = self._required_int(row.get("price_low", row.get("priceLow")), "price_low")
        price_high = self._required_int(row.get("price_high", row.get("priceHigh")), "price_high")
        if price_low >= price_high:
            raise MarketConfigurationError("Frenzy price_low must be less than price_high.")
        interval_start = self._required_int(
            spec.get("interval_start", spec.get("intervalStart")), "interval_start"
        )
        interval_end = self._required_int(spec.get("interval_end", spec.get("intervalEnd")), "interval_end")
        if interval_start >= interval_end:
            raise MarketConfigurationError("Frenzy interval_start must be less than interval_end.")
        bettor = self._address(order.metadata.get("bettor", _ZERO_ADDRESS), label="bettor")
        nonce = self._uint(order.metadata.get("nonce", 0), "nonce")
        deadline = self._uint(order.metadata.get("deadline", int(time.time()) + 300), "deadline")
        amount_raw = int(round(size * self.amount_scale))
        if amount_raw <= 0:
            raise MarketConfigurationError("Frenzy order size is below the configured raw-unit precision.")
        limit_price = self._probability(order.limit_price)
        configured_price = self._probability(row.get("price", row.get("probability")))
        if limit_price is None:
            limit_price = configured_price
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=self._contract_id(event_id, index),
            accepted=True,
            message=(
                f"DRY RUN: would sign Frenzy BetIntent for {size:g} USDC-equivalent in "
                f"price range {price_low}-{price_high}"
            ),
            filled_size=0.0,
            average_price=limit_price,
            raw={
                "dry_run": True,
                "chain_id": self.chain_id,
                "contract_address": self.contract_address,
                "usdc_address": self.usdc_address,
                "amount_raw": amount_raw,
                "signed_transaction_required": True,
                "oracle_ack_required": True,
                "eip712": {
                    "domain": {
                        "name": "FrenzyFinance",
                        "version": "1",
                        "chainId": self.chain_id,
                        "verifyingContract": self.contract_address,
                    },
                    "primaryType": "BetIntent",
                    "types": {
                        "BetIntent": [
                            {"name": "bettor", "type": "address"},
                            {"name": "marketId", "type": "bytes32"},
                            {"name": "intervalStart", "type": "uint64"},
                            {"name": "intervalEnd", "type": "uint64"},
                            {"name": "priceLow", "type": "uint256"},
                            {"name": "priceHigh", "type": "uint256"},
                            {"name": "amount", "type": "uint256"},
                            {"name": "nonce", "type": "uint256"},
                            {"name": "deadline", "type": "uint64"},
                        ]
                    },
                    "message": {
                        "bettor": bettor,
                        "marketId": self._market_id_from_event(event_id),
                        "intervalStart": interval_start,
                        "intervalEnd": interval_end,
                        "priceLow": price_low,
                        "priceHigh": price_high,
                        "amount": amount_raw,
                        "nonce": nonce,
                        "deadline": deadline,
                    },
                },
                "limit_price": limit_price,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            "Frenzy live bets require a wallet signature plus a valid oracle-signed BetAck; the official contract does not expose a public quote/ack service for this adapter.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Frenzy does not publish an account-activity mirroring contract for copy trading.",
        )

    def _settlement_logs(self) -> List[Dict[str, Any]]:
        from_block, to_block = self._block_range()
        payload = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.contract_address,
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "topics": [_BET_SETTLED_TOPIC],
                }
            ],
        )
        if not isinstance(payload, list):
            raise MarketHTTPError("Frenzy eth_getLogs did not return an array.")
        return [self._decode_settlement(row) for row in payload if isinstance(row, Mapping)]

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Frenzy RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Frenzy RPC error: {payload['error']}")
        return payload.get("result")

    def _block_range(self) -> Tuple[str, str]:
        configured_from = self.config.get("frenzy_from_block")
        configured_to = self.config.get("frenzy_to_block")
        if configured_from in (None, ""):
            latest = self._rpc("eth_blockNumber", [])
            configured_from = latest
        if configured_to in (None, ""):
            configured_to = configured_from
        return self._quantity(configured_from, "from block"), self._quantity(configured_to, "to block")

    def _market_specs(self) -> List[Dict[str, Any]]:
        configured: Any = self.config.get("frenzy_market_specs")
        if configured in (None, ""):
            return []
        if isinstance(configured, Mapping):
            configured = [dict(value, market_id=key) if isinstance(value, Mapping) else value for key, value in configured.items()]
        if not isinstance(configured, (list, tuple)):
            raise MarketConfigurationError("Frenzy market specs must be a list or mapping.")
        specs: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in configured:
            if not isinstance(item, Mapping):
                raise MarketConfigurationError("Frenzy market specs must contain objects.")
            market_id = self._bytes32(item.get("market_id", item.get("marketId")), "market id")
            interval_end = self._required_int(item.get("interval_end", item.get("intervalEnd")), "interval_end")
            interval_start = self._required_int(item.get("interval_start", item.get("intervalStart")), "interval_start")
            if interval_start >= interval_end:
                raise MarketConfigurationError("Frenzy interval_start must be less than interval_end.")
            event_id = self._event_id(market_id, interval_end)
            if event_id in seen:
                continue
            seen.add(event_id)
            spec = dict(item)
            spec.update(
                {
                    "market_id": market_id,
                    "interval_start": interval_start,
                    "interval_end": interval_end,
                    "event_id": event_id,
                }
            )
            if item.get("contracts") not in (None, "") and not isinstance(item.get("contracts"), list):
                raise MarketConfigurationError("Frenzy market spec contracts must be a list.")
            specs.append(spec)
        return specs

    def _event_bucket(self, event_id: str) -> Dict[str, Any]:
        canonical = self._canonical_event_id(event_id)
        cached = self._event_cache.get(canonical)
        if cached is not None:
            return cached
        for event in self.list_events(limit=200):
            if event.event_id == canonical:
                return self._event_cache[canonical]
        raise MarketConfigurationError(f"Frenzy event {canonical!r} was not found in configured specs or logs.")

    def _event_from_spec(self, spec: Mapping[str, Any]) -> MarketEvent:
        event_id = str(spec["event_id"])
        market_id = str(spec["market_id"])
        title = str(spec.get("title") or spec.get("question") or f"Frenzy market {market_id[:10]}").strip()
        status = str(spec.get("status") or "active").strip().lower()
        return MarketEvent(
            market_id=self.market_id,
            event_id=event_id,
            title=title,
            url=f"https://frenzy.finance/markets/{market_id}",
            status=status,
            raw={"market_id": market_id, "interval_end": int(spec["interval_end"]), "spec": dict(spec)},
        )

    def _event_from_settlement(self, settlement: Mapping[str, Any]) -> MarketEvent:
        market_id = str(settlement["market_id"])
        interval_end = int(settlement["interval_end"])
        return MarketEvent(
            market_id=self.market_id,
            event_id=self._event_id(market_id, interval_end),
            title=f"Frenzy market {market_id[:10]} interval {interval_end}",
            url=f"https://frenzy.finance/markets/{market_id}",
            status="settled",
            raw={"market_id": market_id, "interval_end": interval_end, "settlement": dict(settlement)},
        )

    @classmethod
    def _decode_settlement(cls, row: Mapping[str, Any]) -> Dict[str, Any]:
        address = cls._address(row.get("address"), label="settlement log address")
        topics = row.get("topics")
        if not isinstance(topics, list) or len(topics) < 4 or str(topics[0]).lower() != _BET_SETTLED_TOPIC:
            raise MarketHTTPError("Frenzy settlement log has an unexpected topic layout.")
        intent_hash = cls._bytes32(topics[1], "intent hash")
        bettor = cls._address("0x" + str(topics[2])[-40:], label="bettor")
        market_id = cls._bytes32(topics[3], "market id")
        data = str(row.get("data") or "0x")
        if not data.startswith("0x") or len(data[2:]) != 4 * 64:
            raise MarketHTTPError("Frenzy BetSettled data must contain four ABI words.")
        words = [int(data[2 + offset : 2 + offset + 64], 16) for offset in range(0, len(data[2:]), 64)]
        return {
            "address": address,
            "intent_hash": intent_hash,
            "bettor": bettor,
            "market_id": market_id,
            "interval_end": words[0],
            "amount_raw": words[1],
            "payout_raw": words[2],
            "won": bool(words[3]),
            "transaction_hash": str(row.get("transactionHash") or ""),
            "block_number": cls._quantity(row.get("blockNumber"), "block number"),
        }

    @classmethod
    def _canonical_event_id(cls, event_id: Any) -> str:
        text = str(event_id or "").strip()
        parts = text.split(":")
        if len(parts) != 3 or parts[0].lower() != "frenzy":
            raise MarketConfigurationError("Frenzy event id must be 'frenzy:<bytes32-market-id>:<interval-end>'.")
        market_id = cls._bytes32(parts[1], "market id")
        interval_end = cls._required_int(parts[2], "interval_end")
        return cls._event_id(market_id, interval_end)

    @classmethod
    def _event_id(cls, market_id: str, interval_end: int) -> str:
        return f"frenzy:{cls._bytes32(market_id, 'market id')}:{int(interval_end)}"

    @staticmethod
    def _contract_id(event_id: str, index: int) -> str:
        return f"{event_id}:{int(index)}"

    @classmethod
    def _split_contract_id(cls, value: Any) -> Tuple[str, int]:
        text = str(value or "").strip()
        if text.count(":") != 3:
            raise MarketConfigurationError("Frenzy contract id must be 'frenzy:<bytes32-market-id>:<interval-end>:<index>'.")
        event_text, index_text = text.rsplit(":", 1)
        if not index_text.isdigit():
            raise MarketConfigurationError("Frenzy contract index must be a non-negative integer.")
        return cls._canonical_event_id(event_text), int(index_text)

    @classmethod
    def _market_id_from_event(cls, event_id: str) -> str:
        return cls._canonical_event_id(event_id).split(":", 2)[1]

    @classmethod
    def _range_label(cls, row: Mapping[str, Any], index: int) -> str:
        low = row.get("price_low", row.get("priceLow"))
        high = row.get("price_high", row.get("priceHigh"))
        if low not in (None, "") and high not in (None, ""):
            return f"{low}-{high}"
        return str(row.get("label") or row.get("outcome") or f"range-{index}")

    @classmethod
    def _address(cls, value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(_ADDRESS_RE, text):
            raise MarketConfigurationError(f"{label} must be a 20-byte hex address.")
        return text

    @classmethod
    def _bytes32(cls, value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not re.fullmatch(_BYTES32_RE, text):
            raise MarketConfigurationError(f"Frenzy {label} must be a 32-byte hex value.")
        return text.lower()

    @classmethod
    def _quantity(cls, value: Any, label: str) -> str:
        number = cls._uint(value, label)
        return hex(number)

    @staticmethod
    def _uint(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise MarketConfigurationError(f"Frenzy {label} must be a non-negative integer.")
        text = str(value or "").strip()
        try:
            number = int(text, 0) if text.lower().startswith("0x") else int(text)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Frenzy {label} must be a non-negative integer.") from exc
        if number < 0:
            raise MarketConfigurationError(f"Frenzy {label} must be a non-negative integer.")
        return number

    @classmethod
    def _required_int(cls, value: Any, label: str) -> int:
        if value in (None, ""):
            raise MarketConfigurationError(f"Frenzy {label} is required.")
        return cls._uint(value, label)

    @staticmethod
    def _positive_float(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number <= 0:
            raise MarketConfigurationError(f"{label} must be positive and finite.")
        return number

    @classmethod
    def _finite_positive(cls, value: Any, label: str) -> float:
        return cls._positive_float(value, label)

    @staticmethod
    def _probability(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and 0 <= number <= 1 else None


__all__ = ["FrenzyFinanceAdapter"]
