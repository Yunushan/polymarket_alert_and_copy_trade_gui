from __future__ import annotations

import base64
import binascii
import re
import struct
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_HEDGEHOG_RPC_URL = "https://mainnetbeta-rpc.eclipse.xyz"
DEFAULT_HEDGEHOG_PROGRAM_ID = "PARrVs6F5egaNuz8g6pKJyU4ze3eX5xGZCFb3GLiVvu"
DEFAULT_HEDGEHOG_AMOUNT_SCALE = 1_000_000
HEDGEHOG_DEPOSIT_DISCRIMINATOR = 4
HEDGEHOG_REFERENCES = (
    "https://github.com/Hedgehog-Markets/hedgehog-program-library",
    "https://github.com/Hedgehog-Markets/hpl-parimutuel-eclipse",
    "https://docs.eclipse.xyz/developers/rpc-and-block-explorers",
)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}
_PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$")
_ACCOUNT_TYPE_MARKET_V1 = 3
_STATE_NAMES = {0: "active", 1: "resolved", 2: "invalid"}


def _base58_encode(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(value) - len(value.lstrip(b"\x00"))
    return "1" * leading_zeroes + (encoded or ("1" if not value else ""))


def _base58_decode(value: str) -> bytes:
    text = str(value or "").strip()
    if not text or not _PUBKEY_RE.fullmatch(text):
        raise MarketConfigurationError("Hedgehog public key must be a canonical base58 value.")
    number = 0
    try:
        for character in text:
            number = number * 58 + _BASE58_INDEX[character]
    except KeyError as exc:
        raise MarketConfigurationError("Hedgehog public key contains an invalid base58 character.") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(text) - len(text.lstrip("1"))
    decoded = b"\x00" * leading_zeroes + raw
    if len(decoded) != 32:
        raise MarketConfigurationError("Hedgehog public key must decode to exactly 32 bytes.")
    return decoded


class HedgehogMarketsAdapter(MarketAdapter):
    """Hedgehog HPL Parimutuel market adapter for Eclipse.

    The official Eclipse program serializes ``MarketV1`` accounts with a small
    custom Borsh layout.  This adapter reads those accounts through the public
    Solana-compatible JSON-RPC endpoint, derives pooled outcome probabilities,
    and emits a dry-run ``DepositV1`` intent.  Live submission is limited to a
    user-signed transaction whose reviewed instruction metadata is checked
    against the selected market, option, amount, and official program before
    forwarding it to the configured Eclipse RPC.  The adapter never signs,
    constructs, or settles a transaction.
    """

    metadata = get_market_metadata("hedgehog_markets")
    live_order_sides = ("BUY",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None) -> None:
        super().__init__(config, runtime=runtime)
        self._market_cache: Dict[str, Dict[str, Any]] = {}

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("hedgehog_rpc_url") or self.config.get("solana_rpc_url")
        value = str(configured or DEFAULT_HEDGEHOG_RPC_URL).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Hedgehog RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def program_id(self) -> str:
        configured = self.config.get("hedgehog_program_id") or DEFAULT_HEDGEHOG_PROGRAM_ID
        return _base58_encode(_base58_decode(str(configured)))

    @property
    def amount_scale(self) -> int:
        value = self.config.get("hedgehog_amount_scale", DEFAULT_HEDGEHOG_AMOUNT_SCALE)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hedgehog amount scale must be a positive integer.") from exc
        if number <= 0:
            raise MarketConfigurationError("Hedgehog amount scale must be a positive integer.")
        return number

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "rpc_url": self.rpc_url,
                "program_id": self.program_id,
                "network": "Eclipse mainnet",
                "references": list(HEDGEHOG_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "account_encoding": "custom Borsh MarketV1",
                "wallet_transaction_required": True,
                "settlement_required": True,
                "live_trading_supported": bool(self.capabilities.live_trading),
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "signed_transaction_submission_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("hedgehog_submit_signed_transactions", False),
                "rpc_configured": bool(
                    self.config.get("hedgehog_rpc_url") or self.config.get("solana_rpc_url")
                ),
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 200))
        payload = self._rpc(
            "getProgramAccounts",
            [
                self.program_id,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "filters": [{"memcmp": {"offset": 0, "bytes": _base58_encode(bytes([_ACCOUNT_TYPE_MARKET_V1]))}}],
                },
            ],
        )
        if not isinstance(payload, list):
            raise MarketHTTPError("Hedgehog getProgramAccounts did not return an array.")
        needle = str(query or "").strip().lower()
        events: List[MarketEvent] = []
        self._market_cache = {}
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            pubkey = self._pubkey(entry.get("pubkey"), label="market account")
            account = entry.get("account")
            if not isinstance(account, Mapping):
                continue
            row = self._decode_account_data(account.get("data"), pubkey)
            self._market_cache[pubkey] = row
            title = self._title(row, pubkey)
            search_text = " ".join((pubkey, title, str(row.get("uri") or ""))).lower()
            if needle and needle not in search_text:
                continue
            events.append(
                MarketEvent(
                    market_id=self.market_id,
                    event_id=pubkey,
                    title=title,
                    url=self._market_url(pubkey),
                    status=self._status(row),
                    raw={"market_account": pubkey, "market": dict(row)},
                )
            )
            if len(events) >= desired:
                break
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        pubkey = self._pubkey(event_id, label="market account")
        row = self._read_market(pubkey)
        title = self._title(row, pubkey)
        status = self._status(row)
        count = len(row["amounts"])
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=f"{pubkey}:{index}",
                event_id=pubkey,
                title=f"{title} - Option {index}",
                outcome=f"Option {index}",
                url=self._market_url(pubkey),
                status=status,
                raw={"market_account": pubkey, "market": dict(row), "option": index},
            )
            for index in range(count)
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        pubkey, option = self._split_contract_id(contract_id)
        row = self._read_market(pubkey)
        amounts = row["amounts"]
        if option >= len(amounts):
            raise MarketConfigurationError("Hedgehog option index is outside the market outcome range.")
        total = sum(amounts)
        price = (amounts[option] / total) if total else None
        canonical = f"{pubkey}:{option}"
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=price,
            midpoint=price,
            source="hedgehog_parimutuel_onchain",
            raw={"market_account": pubkey, "market": dict(row), "option": option, "amounts_total": total},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Hedgehog HPL Parimutuel is a pooled market; the official program does not expose a CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        pubkey, option = self._split_contract_id(order.contract_id)
        side = str(order.side or "").upper()
        if side != "BUY":
            raise MarketConfigurationError("Hedgehog pooled deposits support BUY paper intents only.")
        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError("Hedgehog order size must be positive.")
        limit_price = None
        if order.limit_price is not None:
            limit_price = self._finite_float(order.limit_price, "limit price")
            if limit_price < 0 or limit_price > 1:
                raise MarketConfigurationError("Hedgehog limit price must be between 0 and 1.")
        snapshot = self.get_price(f"{pubkey}:{option}")
        if snapshot.last is None:
            raise MarketConfigurationError("Hedgehog market has no pooled liquidity for a paper order.")
        amount_raw = int(round(size * self.amount_scale))
        if amount_raw <= 0:
            raise MarketConfigurationError("Hedgehog order size is below the configured raw-unit precision.")
        row = self._read_market(pubkey)
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=f"{pubkey}:{option}",
            accepted=True,
            message=f"DRY RUN: would submit Hedgehog DepositV1 intent for {size:g} units at {snapshot.last:.6f}",
            filled_size=0.0,
            average_price=snapshot.last,
            raw={
                "dry_run": True,
                "program_id": self.program_id,
                "rpc_url": self.rpc_url,
                "instruction": "DepositV1",
                "market_account": pubkey,
                "mint": row["mint"],
                "option": option,
                "amount_raw": amount_raw,
                "uri": row.get("uri", ""),
                "limit_price": limit_price,
                "signed_transaction_required": True,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self.ensure_order_market(order)
        audit = self.preflight_live_order(order, feature_name="Hedgehog live trading")
        if not self.config_bool("hedgehog_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "Hedgehog live trading requires hedgehog_submit_signed_transactions=true after reviewing the signed transaction."
            )
        if not (self.config.get("hedgehog_rpc_url") or self.config.get("solana_rpc_url")):
            raise MarketConfigurationError(
                "Hedgehog live orders require hedgehog_rpc_url or solana_rpc_url for transaction submission."
            )

        pubkey, option = self._split_contract_id(order.contract_id)
        row = self._read_market(pubkey)
        if self._status(row) != "active":
            raise MarketConfigurationError("Hedgehog live orders require an active MarketV1 account.")
        size = self._finite_float(order.size, "order size")
        amount_raw = int(round(size * self.amount_scale))
        if amount_raw <= 0:
            raise MarketConfigurationError("Hedgehog order size is below the configured raw-unit precision.")

        metadata = dict(order.metadata or {})
        signed_transaction = str(
            metadata.get("signed_transaction") or metadata.get("signedTransaction") or ""
        ).strip()
        raw_transaction = self._decode_signed_transaction(signed_transaction)
        program_id = self._pubkey(metadata.get("program_id") or metadata.get("programId"), label="program id")
        if program_id != self.program_id:
            raise MarketConfigurationError("Hedgehog signed transaction metadata targets an unexpected program.")
        instruction = str(metadata.get("instruction") or "").strip()
        if instruction != "DepositV1":
            raise MarketConfigurationError("Hedgehog live orders require reviewed DepositV1 instruction metadata.")
        market_account = self._pubkey(
            metadata.get("market_account") or metadata.get("marketAccount"), label="market account"
        )
        if market_account != pubkey:
            raise MarketConfigurationError("Hedgehog signed transaction metadata targets a different market account.")
        try:
            reviewed_option = int(metadata.get("option"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hedgehog live orders require a reviewed integer option metadata field.") from exc
        if reviewed_option != option:
            raise MarketConfigurationError("Hedgehog signed transaction metadata targets a different outcome option.")
        try:
            reviewed_amount = int(metadata.get("amount_raw"))
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Hedgehog live orders require a reviewed amount_raw metadata field.") from exc
        if reviewed_amount != amount_raw:
            raise MarketConfigurationError("Hedgehog signed transaction amount does not match the requested order size.")
        instruction_data = str(metadata.get("instruction_data") or "").strip().lower()
        if instruction_data.startswith("0x"):
            instruction_data = instruction_data[2:]
        expected_data = bytes([HEDGEHOG_DEPOSIT_DISCRIMINATOR, option]) + amount_raw.to_bytes(8, "little")
        if instruction_data != expected_data.hex():
            raise MarketConfigurationError(
                "Hedgehog live orders require reviewed DepositV1 instruction_data matching option and amount_raw."
            )

        signature = self._rpc(
            "sendTransaction",
            [signed_transaction, {"encoding": "base64", "skipPreflight": False}],
        )
        if not isinstance(signature, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,128}", signature):
            raise MarketHTTPError("Hedgehog RPC did not return a valid transaction signature.")
        return {
            "market_id": self.market_id,
            "contract_id": f"{pubkey}:{option}",
            "live": True,
            "preflight": audit,
            "submission": "solana_rpc_sendTransaction",
            "signature": signature,
            "program_id": self.program_id,
            "instruction": "DepositV1",
            "market_account": pubkey,
            "option": option,
            "amount_raw": amount_raw,
            "signed_transaction_bytes": len(raw_transaction),
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Hedgehog does not publish an account-activity mirroring contract for copy trading.",
        )

    def _read_market(self, pubkey: str) -> Dict[str, Any]:
        canonical = self._pubkey(pubkey, label="market account")
        cached = self._market_cache.get(canonical)
        if cached is not None:
            return cached
        payload = self._rpc("getAccountInfo", [canonical, {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Hedgehog getAccountInfo did not return an object.")
        value = payload.get("value")
        if not isinstance(value, Mapping):
            raise MarketHTTPError("Hedgehog market account was not found.")
        row = self._decode_account_data(value.get("data"), canonical)
        self._market_cache[canonical] = row
        return row

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Hedgehog RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Hedgehog RPC error: {payload['error']}")
        return payload.get("result")

    @staticmethod
    def _decode_signed_transaction(value: str) -> bytes:
        if not value or len(value) > 1_400_000 or len(value) % 4:
            raise MarketConfigurationError(
                "Hedgehog live orders require a canonical base64 wallet-signed transaction."
            )
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MarketConfigurationError(
                "Hedgehog live orders require a canonical base64 wallet-signed transaction."
            ) from exc
        if len(raw) < 64 or len(raw) > 1_000_000:
            raise MarketConfigurationError("Hedgehog signed transaction has an invalid size.")
        if base64.b64encode(raw).decode("ascii") != value:
            raise MarketConfigurationError("Hedgehog signed transaction must use canonical base64 encoding.")
        return raw

    @classmethod
    def _pubkey(cls, value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        return _base58_encode(_base58_decode(text)) if text else cls._missing_key(label)

    @staticmethod
    def _missing_key(label: str) -> str:
        raise MarketConfigurationError(f"Hedgehog {label} is required.")

    @classmethod
    def _split_contract_id(cls, value: Any) -> Tuple[str, int]:
        text = str(value or "").strip()
        if text != str(value or "") or text.count(":") != 1:
            raise MarketConfigurationError("Hedgehog contract id must be '<market-account>:<option-index>'.")
        pubkey_text, option_text = text.split(":", 1)
        if not option_text.isdigit():
            raise MarketConfigurationError("Hedgehog contract option index must be a non-negative integer.")
        option = int(option_text)
        if option > 255:
            raise MarketConfigurationError("Hedgehog contract option index is outside the supported range.")
        return cls._pubkey(pubkey_text, label="market account"), option

    @staticmethod
    def _market_url(pubkey: str) -> str:
        return f"https://hedgehog.markets/markets/{pubkey}"

    @staticmethod
    def _title(row: Mapping[str, Any], pubkey: str) -> str:
        index = row.get("index")
        return f"Hedgehog market {index}" if index is not None else f"Hedgehog market {pubkey[:8]}"

    @staticmethod
    def _status(row: Mapping[str, Any]) -> str:
        state = int(row.get("state", -1))
        if state == 0 and int(row.get("close_timestamp", 0)) > 0 and time.time() >= int(row["close_timestamp"]):
            return "closed"
        return _STATE_NAMES.get(state, f"unknown:{state}")

    @classmethod
    def _decode_account_data(cls, data: Any, pubkey: str) -> Dict[str, Any]:
        if not isinstance(data, (list, tuple)) or len(data) < 2 or data[1] != "base64" or not isinstance(data[0], str):
            raise MarketHTTPError(f"Hedgehog account {pubkey} did not return base64 account data.")
        try:
            raw = base64.b64decode(data[0], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MarketHTTPError(f"Hedgehog account {pubkey} returned invalid base64 data.") from exc
        if len(raw) > 1_000_000:
            raise MarketHTTPError(f"Hedgehog account {pubkey} is unexpectedly large.")
        offset = 0

        def take(size: int) -> bytes:
            nonlocal offset
            if size < 0 or offset + size > len(raw):
                raise MarketHTTPError(f"Hedgehog account {pubkey} is truncated for the MarketV1 layout.")
            value = raw[offset : offset + size]
            offset += size
            return value

        def u8() -> int:
            return take(1)[0]

        def u16() -> int:
            return struct.unpack("<H", take(2))[0]

        def u32() -> int:
            return struct.unpack("<I", take(4))[0]

        def i64() -> int:
            return struct.unpack("<q", take(8))[0]

        def key() -> str:
            return _base58_encode(take(32))

        account_type = u8()
        if account_type != _ACCOUNT_TYPE_MARKET_V1:
            raise MarketHTTPError(f"Hedgehog account {pubkey} is not a MarketV1 account.")
        creator = key()
        index = u32()
        resolver = key()
        mint = key()
        close_timestamp = i64()
        resolve_timestamp = i64()
        outcome_timestamp = i64()
        creator_fee_bps = u16()
        platform_fee_bps = u16()
        state = u8()
        outcome = u8()
        amount_count = u8()
        if amount_count > 64:
            raise MarketHTTPError(f"Hedgehog account {pubkey} has an invalid outcome count.")
        amounts = [struct.unpack("<Q", take(8))[0] for _ in range(amount_count)]
        uri_length = u32()
        if uri_length > 16_384:
            raise MarketHTTPError(f"Hedgehog account {pubkey} has an invalid URI length.")
        try:
            uri = take(uri_length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MarketHTTPError(f"Hedgehog account {pubkey} contains an invalid UTF-8 URI.") from exc
        if state not in _STATE_NAMES:
            raise MarketHTTPError(f"Hedgehog account {pubkey} has an unknown MarketV1 state.")
        return {
            "account_type": account_type,
            "creator": creator,
            "index": index,
            "resolver": resolver,
            "mint": mint,
            "close_timestamp": close_timestamp,
            "resolve_timestamp": resolve_timestamp,
            "outcome_timestamp": outcome_timestamp,
            "creator_fee_bps": creator_fee_bps,
            "platform_fee_bps": platform_fee_bps,
            "state": state,
            "outcome": outcome,
            "amounts": amounts,
            "uri": uri,
            "encoded_length": len(raw),
        }


__all__ = ["HedgehogMarketsAdapter", "HEDGEHOG_REFERENCES"]
