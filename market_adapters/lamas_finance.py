from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .base import MarketAdapter
from .catalog import get_market_metadata
from .errors import MarketConfigurationError, MarketHTTPError, UnsupportedFeatureError
from .types import MarketContract, MarketEvent, PaperOrderRequest, PaperOrderResult, PriceSnapshot


DEFAULT_LAMAS_RPC_URL = "https://api.devnet.solana.com"
DEFAULT_LAMAS_PRICE_PROGRAM_ID = "SqFtqbsedB3HVwYd89wxTwL4UXV5JASq3JoPcApjNMJ"
DEFAULT_LAMAS_UP_DOWN_PROGRAM_ID = "BbCEshx6obrBjzWPXBRxq99GcFVPB8ioe48pUYr711zy"
DEFAULT_LAMAS_PRICE_MINT = "HEy1zzM3LFUFy8TbHEoEKQMYmPx1Uz3o51RZYRqCtDrs"
DEFAULT_LAMAS_UP_DOWN_MINT = "9a7TwLHkA2AaJd9E7qsdhaTPhQL5wQ9VXYo7J2pXHixV"
DEFAULT_LAMAS_AMOUNT_SCALE = 1_000_000_000
DEFAULT_LAMAS_PRICE_SCALE = 1_000_000_000_000
LAMAS_REFERENCES = (
    "https://docs.lamas.co/1.0",
    "https://github.com/LamasFinance/LamasFinance",
    "https://solana.com/docs/rpc/http/getprogramaccounts",
)

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {character: index for index, character in enumerate(_BASE58_ALPHABET)}
_PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$")
_STAGES = {0: "upcoming", 1: "active", 2: "live", 3: "resolved", 4: "canceled"}
_ACCOUNT_DISCRIMINATORS = {
    "up_or_down": hashlib.sha256(b"account:RoundResult").digest()[:8],
    "price_predict": hashlib.sha256(b"account:RoundResult").digest()[:8],
}
_PREDICT_DISCRIMINATOR = hashlib.sha256(b"global:predict").digest()[:8]


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
        raise MarketConfigurationError("Lamas public key must be a canonical base58 value.")
    number = 0
    try:
        for character in text:
            number = number * 58 + _BASE58_INDEX[character]
    except KeyError as exc:
        raise MarketConfigurationError("Lamas public key contains an invalid base58 character.") from exc
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(text) - len(text.lstrip("1"))
    decoded = b"\x00" * leading_zeroes + raw
    if len(decoded) != 32:
        raise MarketConfigurationError("Lamas public key must decode to exactly 32 bytes.")
    if _base58_encode(decoded) != text:
        raise MarketConfigurationError("Lamas public key is not canonical base58.")
    return decoded


def _discriminator_base58(value: bytes) -> str:
    return _base58_encode(value)


class LamasFinanceAdapter(MarketAdapter):
    """Fixture-backed adapter for the official Lamas Finance Solana programs.

    The public Lamas repository documents the ``PricePredict`` and
    ``UpOrDown`` Anchor programs and their devnet deployment identifiers.  The
    adapter reads ``RoundResult`` accounts through Solana JSON-RPC, derives
    pooled YES/NO probabilities for ``UpOrDown``, exposes the price-prediction
    round reference price, and emits unsigned ``predict`` intents.  Live
    submission accepts only a canonical wallet-signed transaction whose
    reviewed program, round, outcome, amount, and Anchor instruction bytes
    match the request.  The adapter never signs, settles, or mirrors accounts.
    """

    metadata = get_market_metadata("lamas_finance")
    live_order_sides = ("BUY",)

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *, runtime=None, clock=None) -> None:
        super().__init__(config, runtime=runtime)
        self._event_cache: Dict[str, Dict[str, Any]] = {}
        self._clock = clock or time.time

    @property
    def rpc_url(self) -> str:
        configured = self.config.get("lamas_finance_rpc_url") or self.config.get("solana_rpc_url")
        value = str(configured or DEFAULT_LAMAS_RPC_URL).strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise MarketConfigurationError("Lamas RPC URL must be an absolute http(s) URL without query or fragment.")
        return value

    @property
    def cluster(self) -> str:
        value = str(self.config.get("lamas_finance_cluster") or "devnet").strip().lower()
        if value not in {"devnet", "testnet", "mainnet-beta"}:
            raise MarketConfigurationError("Lamas cluster must be devnet, testnet, or mainnet-beta.")
        return value

    @property
    def amount_scale(self) -> int:
        value = self.config.get("lamas_finance_amount_scale", DEFAULT_LAMAS_AMOUNT_SCALE)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Lamas amount scale must be a positive integer.") from exc
        if number <= 0:
            raise MarketConfigurationError("Lamas amount scale must be a positive integer.")
        return number

    @property
    def price_scale(self) -> int:
        value = self.config.get("lamas_finance_price_scale", DEFAULT_LAMAS_PRICE_SCALE)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Lamas price scale must be a positive integer.") from exc
        if number <= 0:
            raise MarketConfigurationError("Lamas price scale must be a positive integer.")
        return number

    @property
    def price_program_id(self) -> str:
        return self._program_id("price_predict", self.config.get("lamas_finance_price_program_id"))

    @property
    def up_or_down_program_id(self) -> str:
        return self._program_id("up_or_down", self.config.get("lamas_finance_up_down_program_id"))

    def health_check(self) -> Dict[str, Any]:
        health = super().health_check()
        health.update(
            {
                "rpc_url": self.rpc_url,
                "cluster": self.cluster,
                "price_predict_program_id": self.price_program_id,
                "up_or_down_program_id": self.up_or_down_program_id,
                "price_predict_mint": self._pubkey(
                    self.config.get("lamas_finance_price_mint") or DEFAULT_LAMAS_PRICE_MINT,
                    label="price-predict mint",
                ),
                "up_or_down_mint": self._pubkey(
                    self.config.get("lamas_finance_up_down_mint") or DEFAULT_LAMAS_UP_DOWN_MINT,
                    label="up-or-down mint",
                ),
                "references": list(LAMAS_REFERENCES),
                "public_api": False,
                "onchain_reading": True,
                "account_encoding": "Anchor RoundResult account",
                "wallet_transaction_required": True,
                "settlement_required": True,
                "live_trading_supported": bool(self.capabilities.live_trading),
                "live_trading_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("live_trading_enabled", False),
                "signed_transaction_submission_enabled": bool(self.capabilities.live_trading)
                and self.config_bool("lamas_finance_submit_signed_transactions", False),
                "rpc_configured": bool(
                    self.config.get("lamas_finance_rpc_url") or self.config.get("solana_rpc_url")
                ),
                "copy_trading_supported": False,
            }
        )
        return health

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        desired = max(1, min(int(limit or 50), 200))
        needle = str(query or "").strip().lower()
        self._event_cache = {}
        events: List[MarketEvent] = []
        for game in ("up_or_down", "price_predict"):
            discriminator = _ACCOUNT_DISCRIMINATORS[game]
            rows = self._program_accounts(
                self._program_id(game, None),
                discriminator,
            )
            for row in rows:
                pubkey = self._pubkey(row.get("pubkey"), label="round account")
                account = row.get("account")
                if not isinstance(account, Mapping):
                    continue
                decoded = self._decode_account_data(account.get("data"), game, pubkey)
                event_id = f"{game}:{pubkey}"
                self._event_cache[event_id] = decoded
                title = self._title(game, decoded, pubkey)
                search_text = f"{game} {pubkey} {title}".lower()
                if needle and needle not in search_text:
                    continue
                events.append(
                    MarketEvent(
                        market_id=self.market_id,
                        event_id=event_id,
                        title=title,
                        url=self._event_url(pubkey),
                        status=self._status(game, decoded),
                        raw={"program": game, "round_account": pubkey, "round": dict(decoded)},
                    )
                )
                if len(events) >= desired:
                    return events
        return events

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        game, pubkey = self._split_event_id(event_id)
        row = self._read_event(game, pubkey)
        status = self._status(game, row)
        title = self._title(game, row, pubkey)
        if game == "up_or_down":
            return [
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"{game}:{pubkey}:YES",
                    event_id=f"{game}:{pubkey}",
                    title=f"{title} - Up",
                    outcome="YES",
                    url=self._event_url(pubkey),
                    status=status,
                    raw={"program": game, "round_account": pubkey, "round": dict(row), "is_up": True},
                ),
                MarketContract(
                    market_id=self.market_id,
                    contract_id=f"{game}:{pubkey}:NO",
                    event_id=f"{game}:{pubkey}",
                    title=f"{title} - Down",
                    outcome="NO",
                    url=self._event_url(pubkey),
                    status=status,
                    raw={"program": game, "round_account": pubkey, "round": dict(row), "is_up": False},
                ),
            ]
        return [
            MarketContract(
                market_id=self.market_id,
                contract_id=f"{game}:{pubkey}:REFERENCE",
                event_id=f"{game}:{pubkey}",
                title=f"{title} - Reference price",
                outcome="REFERENCE",
                url=self._event_url(pubkey),
                status=status,
                raw={"program": game, "round_account": pubkey, "round": dict(row)},
            )
        ]

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        game, pubkey, outcome = self._split_contract_id(contract_id)
        row = self._read_event(game, pubkey)
        canonical = f"{game}:{pubkey}:{outcome}"
        if game == "up_or_down":
            if outcome not in {"YES", "NO"}:
                raise MarketConfigurationError("Lamas UpOrDown outcome must be YES or NO.")
            up = int(row["up_pool_value"])
            down = int(row["down_pool_value"])
            total = up + down
            price = None if total <= 0 else (up if outcome == "YES" else down) / total
            return PriceSnapshot(
                market_id=self.market_id,
                contract_id=canonical,
                last=price,
                midpoint=price,
                source="lamas_up_or_down_pools",
                raw={"program": game, "round_account": pubkey, "round": dict(row), "pool_total": total},
            )
        if outcome != "REFERENCE":
            raise MarketConfigurationError("Lamas PricePredict outcome must be REFERENCE.")
        reference = row.get("price_end_stage") or row.get("price_start_stage")
        value = self._decimal_float(reference)
        return PriceSnapshot(
            market_id=self.market_id,
            contract_id=canonical,
            last=value,
            midpoint=value,
            source="lamas_price_predict_reference",
            raw={"program": game, "round_account": pubkey, "round": dict(row)},
        )

    def get_orderbook(self, contract_id: str):
        raise UnsupportedFeatureError(
            self.market_id,
            "orderbook_reading",
            "Lamas Finance uses pooled Solana game contracts; the official programs do not expose a CLOB orderbook.",
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        self.ensure_order_market(order)
        game, pubkey, outcome = self._split_contract_id(order.contract_id)
        if str(order.side or "").upper() != "BUY":
            raise MarketConfigurationError("Lamas Finance prediction intents support BUY only.")
        row = self._read_event(game, pubkey)
        if not self._is_open(game, row):
            raise MarketConfigurationError("Lamas Finance paper orders require an open prediction round.")
        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError("Lamas order size must be positive.")
        amount_raw = int(round(size * self.amount_scale))
        self._validate_amount_raw(amount_raw)
        minimum = int(row.get("min_bet_amount") or self.config.get("lamas_finance_min_bet_amount") or 0)
        if minimum and amount_raw < minimum:
            raise MarketConfigurationError("Lamas order size is below the round's minimum bet amount.")
        limit_price = self._validate_limit_price(order.limit_price)
        intent = self._build_intent(game, pubkey, outcome, amount_raw, order.metadata)
        snapshot = self.get_price(f"{game}:{pubkey}:{outcome}")
        return PaperOrderResult(
            market_id=self.market_id,
            contract_id=f"{game}:{pubkey}:{outcome}",
            accepted=True,
            message=f"DRY RUN: would submit Lamas {game} predict intent for {size:g} units",
            filled_size=0.0,
            average_price=snapshot.last,
            raw={
                "dry_run": True,
                "rpc_url": self.rpc_url,
                "program_id": self._program_id(game, None),
                "round_account": pubkey,
                "amount_raw": amount_raw,
                "limit_price": limit_price,
                "signed_transaction_required": True,
                **intent,
            },
        )

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        self.ensure_order_market(order)
        audit = self.preflight_live_order(order, feature_name="Lamas Finance live trading")
        if not self.config_bool("lamas_finance_submit_signed_transactions", False):
            raise MarketConfigurationError(
                "Lamas live trading requires lamas_finance_submit_signed_transactions=true after reviewing the signed transaction."
            )
        if not (self.config.get("lamas_finance_rpc_url") or self.config.get("solana_rpc_url")):
            raise MarketConfigurationError(
                "Lamas live orders require lamas_finance_rpc_url or solana_rpc_url for transaction submission."
            )
        game, pubkey, outcome = self._split_contract_id(order.contract_id)
        row = self._read_event(game, pubkey)
        if not self._is_open(game, row):
            raise MarketConfigurationError("Lamas live orders require an open prediction round.")
        size = self._finite_float(order.size, "order size")
        amount_raw = int(round(size * self.amount_scale))
        self._validate_amount_raw(amount_raw)
        minimum = int(row.get("min_bet_amount") or self.config.get("lamas_finance_min_bet_amount") or 0)
        if minimum and amount_raw < minimum:
            raise MarketConfigurationError("Lamas order size is below the round's minimum bet amount.")
        metadata = dict(order.metadata or {})
        signed = str(metadata.get("signed_transaction") or metadata.get("signedTransaction") or "").strip()
        raw_transaction = self._decode_signed_transaction(signed)
        program_id = self._pubkey(metadata.get("program_id") or metadata.get("programId"), label="program id")
        expected_program = self._program_id(game, None)
        if program_id != expected_program:
            raise MarketConfigurationError("Lamas signed transaction metadata targets an unexpected program.")
        if _base58_decode(expected_program) not in raw_transaction:
            raise MarketConfigurationError("Lamas signed transaction does not contain the reviewed program address.")
        if str(metadata.get("instruction") or "").strip() != "predict":
            raise MarketConfigurationError("Lamas live orders require reviewed predict instruction metadata.")
        market_account = self._pubkey(
            metadata.get("round_account") or metadata.get("roundAccount"), label="round account"
        )
        if market_account != pubkey:
            raise MarketConfigurationError("Lamas signed transaction targets a different round account.")
        intent = self._build_intent(game, pubkey, outcome, amount_raw, metadata)
        if _base58_decode(pubkey) not in raw_transaction:
            raise MarketConfigurationError("Lamas signed transaction does not contain the reviewed round account.")
        if bytes.fromhex(intent["instruction_data"]) not in raw_transaction:
            raise MarketConfigurationError("Lamas signed transaction does not contain the reviewed predict instruction.")
        reviewed_data = str(metadata.get("instruction_data") or "").strip().lower()
        if reviewed_data.startswith("0x"):
            reviewed_data = reviewed_data[2:]
        if reviewed_data != intent["instruction_data"]:
            raise MarketConfigurationError("Lamas signed transaction instruction_data does not match the reviewed intent.")
        response = self._rpc("sendTransaction", [signed, {"encoding": "base64", "skipPreflight": False}])
        if not isinstance(response, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,128}", response):
            raise MarketHTTPError("Lamas RPC did not return a valid transaction signature.")
        return {
            "market_id": self.market_id,
            "contract_id": f"{game}:{pubkey}:{outcome}",
            "live": True,
            "preflight": audit,
            "submission": "solana_rpc_sendTransaction",
            "signature": response,
            "program_id": expected_program,
            "round_account": pubkey,
            "instruction": "predict",
            "instruction_data": intent["instruction_data"],
            "amount_raw": amount_raw,
            "signed_transaction_bytes": len(raw_transaction),
        }

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        raise UnsupportedFeatureError(
            self.market_id,
            "copy_trading",
            "Lamas Finance does not publish an account-activity mirroring API or protocol contract.",
        )

    def _program_accounts(self, program_id: str, discriminator: bytes) -> List[Mapping[str, Any]]:
        result = self._rpc(
            "getProgramAccounts",
            [
                program_id,
                {
                    "encoding": "base64",
                    "commitment": "confirmed",
                    "filters": [{"memcmp": {"offset": 0, "bytes": _discriminator_base58(discriminator)}}],
                },
            ],
        )
        if not isinstance(result, list):
            raise MarketHTTPError("Lamas getProgramAccounts did not return an array.")
        return [row for row in result if isinstance(row, Mapping)]

    def _read_event(self, game: str, pubkey: str) -> Dict[str, Any]:
        event_id = f"{game}:{pubkey}"
        cached = self._event_cache.get(event_id)
        if cached is not None:
            return cached
        result = self._rpc("getAccountInfo", [pubkey, {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(result, Mapping) or not isinstance(result.get("value"), Mapping):
            raise MarketHTTPError("Lamas round account was not found.")
        owner = result["value"].get("owner")
        expected_program = self._program_id(game, None)
        if owner is not None and self._pubkey(owner, label="round account owner") != expected_program:
            raise MarketHTTPError("Lamas round account is owned by an unexpected program.")
        row = self._decode_account_data(result["value"].get("data"), game, pubkey)
        self._event_cache[event_id] = row
        return row

    def _rpc(self, method: str, params: List[Any]) -> Any:
        payload = self.runtime.request_json(
            "POST",
            self.rpc_url,
            json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if not isinstance(payload, Mapping):
            raise MarketHTTPError("Lamas RPC response was not a JSON object.")
        if payload.get("error"):
            raise MarketHTTPError(f"Lamas RPC error: {payload['error']}")
        return payload.get("result")

    @classmethod
    def _decode_account_data(cls, data: Any, game: str, pubkey: str) -> Dict[str, Any]:
        if not isinstance(data, (list, tuple)) or len(data) < 2 or data[1] != "base64" or not isinstance(data[0], str):
            raise MarketHTTPError(f"Lamas {game} account {pubkey} did not return base64 account data.")
        try:
            raw = base64.b64decode(data[0], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MarketHTTPError(f"Lamas {game} account {pubkey} returned invalid base64 data.") from exc
        if len(raw) > 1_000_000:
            raise MarketHTTPError(f"Lamas {game} account {pubkey} is unexpectedly large.")
        expected = _ACCOUNT_DISCRIMINATORS[game]
        if raw[:8] != expected:
            raise MarketHTTPError(f"Lamas {game} account {pubkey} is not an Anchor RoundResult account.")
        offset = 8

        def take(size: int) -> bytes:
            nonlocal offset
            if size < 0 or offset + size > len(raw):
                raise MarketHTTPError(f"Lamas {game} account {pubkey} is truncated for the RoundResult layout.")
            value = raw[offset : offset + size]
            offset += size
            return value

        def u8() -> int:
            return take(1)[0]

        def u64() -> int:
            return struct.unpack("<Q", take(8))[0]

        def i128() -> int:
            return int.from_bytes(take(16), "little", signed=True)

        def key() -> str:
            return _base58_encode(take(32))

        def decimal() -> Dict[str, int]:
            return {"value": i128(), "decimals": struct.unpack("<I", take(4))[0]}

        if game == "up_or_down":
            row = {
                "game": game,
                "round_index": u64(),
                "pool": key(),
                "up_pool_value": u64(),
                "down_pool_value": u64(),
                "did_up_win": bool(u8()),
                "min_bet_amount": u64(),
                "profit_tax_percentage": u64(),
                "tax_burn_percentage": u64(),
                "price_end_predict_stage": decimal(),
                "price_end_live_stage": decimal(),
                "unix_time_start_round": u64(),
                "unix_time_start_live_stage": u64(),
                "unix_time_end_live_stage": u64(),
                "stage": u8(),
            }
            if row["stage"] not in _STAGES:
                raise MarketHTTPError(f"Lamas UpOrDown account {pubkey} has an unknown stage.")
            return row

        row = {
            "game": game,
            "pool": key(),
            "price_start_stage": {"value": int.from_bytes(take(16), "little"), "decimals": 12},
            "price_end_stage": {"value": int.from_bytes(take(16), "little"), "decimals": 12},
            "sum_stake": int.from_bytes(take(16), "little"),
            "sum_stake_mul_score": int.from_bytes(take(16), "little"),
            "result_vec0": struct.unpack("<d", take(8))[0],
            "unix_time_start_round": u64(),
            "unix_time_end_round": u64(),
            "finalized": u8(),
        }
        if row["finalized"] not in {0, 1}:
            raise MarketHTTPError(f"Lamas PricePredict account {pubkey} has an invalid finalized flag.")
        return row

    def _build_intent(
        self,
        game: str,
        pubkey: str,
        outcome: str,
        amount_raw: int,
        metadata: Mapping[str, Any],
    ) -> Dict[str, Any]:
        program_id = self._program_id(game, None)
        if game == "up_or_down":
            if outcome not in {"YES", "NO"}:
                raise MarketConfigurationError("Lamas UpOrDown outcome must be YES or NO.")
            is_up = outcome == "YES"
            instruction_data = (_PREDICT_DISCRIMINATOR + bytes([1 if is_up else 0]) + amount_raw.to_bytes(8, "little")).hex()
            return {
                "instruction": "predict",
                "program_id": program_id,
                "game": game,
                "round_account": pubkey,
                "outcome": outcome,
                "is_up": is_up,
                "amount_raw": amount_raw,
                "instruction_data": instruction_data,
            }
        if outcome != "REFERENCE":
            raise MarketConfigurationError("Lamas PricePredict outcome must be REFERENCE.")
        raw_price_value = metadata.get("predict_price_raw")
        if raw_price_value is None:
            predict_price = metadata.get("predict_price")
            if predict_price is None:
                raise MarketConfigurationError(
                    "Lamas PricePredict paper/live intents require metadata['predict_price'] or ['predict_price_raw']."
                )
            try:
                predict_price_value = float(predict_price)
            except (TypeError, ValueError) as exc:
                raise MarketConfigurationError("Lamas predict_price must be numeric.") from exc
            if predict_price_value <= 0:
                raise MarketConfigurationError("Lamas predict_price must be positive.")
            raw_price_value = int(round(predict_price_value * self.price_scale))
        try:
            raw_price = int(raw_price_value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Lamas predict_price_raw must be an integer.") from exc
        if raw_price <= 0 or raw_price >= 1 << 128:
            raise MarketConfigurationError("Lamas predict_price_raw is outside the u128 range.")
        instruction_data = (_PREDICT_DISCRIMINATOR + amount_raw.to_bytes(8, "little") + raw_price.to_bytes(16, "little")).hex()
        return {
            "instruction": "predict",
            "program_id": program_id,
            "game": game,
            "round_account": pubkey,
            "outcome": outcome,
            "amount_raw": amount_raw,
            "predict_price_raw": raw_price,
            "instruction_data": instruction_data,
        }

    @staticmethod
    def _decode_signed_transaction(value: str) -> bytes:
        if not value or len(value) > 1_400_000 or len(value) % 4:
            raise MarketConfigurationError("Lamas live orders require a canonical base64 wallet-signed transaction.")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MarketConfigurationError("Lamas live orders require a canonical base64 wallet-signed transaction.") from exc
        if len(raw) < 64 or len(raw) > 1_000_000:
            raise MarketConfigurationError("Lamas signed transaction has an invalid size.")
        if base64.b64encode(raw).decode("ascii") != value:
            raise MarketConfigurationError("Lamas signed transaction must use canonical base64 encoding.")
        return raw

    @classmethod
    def _split_event_id(cls, value: Any) -> Tuple[str, str]:
        text = str(value or "")
        if text != text.strip() or text.count(":") != 1:
            raise MarketConfigurationError("Lamas event id must be '<program>:<round-account>'.")
        game, pubkey = text.split(":", 1)
        if game not in {"up_or_down", "price_predict"}:
            raise MarketConfigurationError("Lamas event id has an unknown program.")
        return game, cls._pubkey(pubkey, label="round account")

    @classmethod
    def _split_contract_id(cls, value: Any) -> Tuple[str, str, str]:
        text = str(value or "")
        if text != text.strip() or text.count(":") != 2:
            raise MarketConfigurationError("Lamas contract id must be '<program>:<round-account>:<outcome>'.")
        game, pubkey, outcome = text.split(":", 2)
        if game not in {"up_or_down", "price_predict"}:
            raise MarketConfigurationError("Lamas contract id has an unknown program.")
        return game, cls._pubkey(pubkey, label="round account"), outcome.strip().upper()

    @classmethod
    def _pubkey(cls, value: Any, *, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise MarketConfigurationError(f"Lamas {label} is required.")
        return _base58_encode(_base58_decode(text))

    @classmethod
    def _program_id(cls, game: str, configured: Any) -> str:
        value = configured
        if value is None:
            value = DEFAULT_LAMAS_PRICE_PROGRAM_ID if game == "price_predict" else DEFAULT_LAMAS_UP_DOWN_PROGRAM_ID
        return _base58_encode(_base58_decode(str(value)))

    @staticmethod
    def _decimal_float(value: Any) -> Optional[float]:
        if not isinstance(value, Mapping):
            return None
        try:
            integer = int(value.get("value"))
            decimals = int(value.get("decimals"))
        except (TypeError, ValueError):
            return None
        if decimals < 0 or decimals > 38:
            return None
        return integer / (10**decimals)

    @staticmethod
    def _validate_limit_price(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError("Lamas probability limit_price must be numeric.") from exc
        if not (number == number and abs(number) != float("inf")):
            raise MarketConfigurationError("Lamas probability limit_price must be finite.")
        if number < 0 or number > 1:
            raise MarketConfigurationError("Lamas probability limit_price must be between 0 and 1.")
        return number

    @staticmethod
    def _validate_amount_raw(amount_raw: int) -> None:
        if amount_raw <= 0:
            raise MarketConfigurationError("Lamas order size is below the configured raw-unit precision.")
        if amount_raw >= 1 << 64:
            raise MarketConfigurationError("Lamas order size exceeds the u64 raw-unit range.")

    def _status(self, game: str, row: Mapping[str, Any]) -> str:
        if game == "up_or_down":
            stage = int(row.get("stage", -1))
            status = _STAGES.get(stage, f"unknown:{stage}")
            end = int(row.get("unix_time_end_live_stage") or 0)
            if status in {"active", "live"} and end and self._clock() >= end:
                return "closed"
            return status
        return "resolved" if int(row.get("finalized", 0)) else "active"

    def _is_open(self, game: str, row: Mapping[str, Any]) -> bool:
        return self._status(game, row) in {"active", "live"}

    @staticmethod
    def _title(game: str, row: Mapping[str, Any], pubkey: str) -> str:
        if game == "up_or_down":
            return f"Lamas Up or Down round {row.get('round_index', pubkey[:8])}"
        return f"Lamas Price Prediction round {pubkey[:8]}"

    def _event_url(self, pubkey: str) -> str:
        return f"https://explorer.solana.com/address/{pubkey}?cluster={self.cluster}"


__all__ = ["LamasFinanceAdapter", "LAMAS_REFERENCES"]
