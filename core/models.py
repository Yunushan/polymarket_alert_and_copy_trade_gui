from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Literal, Optional, Dict, Any, List, cast
import hashlib
import json
import math
import uuid
import time

from market_adapters.catalog import MARKET_CATALOG


PriceSource = Literal["last_trade", "midpoint", "best_bid", "best_ask"]
Direction = Literal["above", "below"]
Theme = Literal["light", "dark"]
UIDesign = Literal["classic", "aurora_2026", "graphite_2026", "sentinel_2027"]
CopyActivityState = Literal["pending", "retryable", "completed", "rejected", "ambiguous"]
MutationJournalState = Literal["pending", "retryable", "completed", "rejected", "ambiguous"]
DEFAULT_MARKET_ID = "polymarket"
DEFAULT_UI_DESIGN: UIDesign = "aurora_2026"
MAX_MUTATION_JOURNAL_ENTRIES = 256
MAX_MUTATION_RESULT_BYTES = 256 * 1024


def _uuid() -> str:
    return str(uuid.uuid4())


def _config_bool(data: Dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"Configuration field '{key}' must be a JSON boolean.")
    return value


def _config_number(value: Any, key: str, minimum: float, maximum: float | None = None, *, positive: bool = False) -> float:
    if type(value) not in (int, float, str):
        raise ValueError(f"Configuration field '{key}' must be a finite number.")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Configuration field '{key}' must be a finite number.") from exc
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum) or (positive and number <= 0):
        raise ValueError(f"Configuration field '{key}' is outside its supported range.")
    return number


def _config_integer(value: Any, key: str, maximum: int | None = None) -> int:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"Configuration field '{key}' must be an integer in its supported range.")
    return int(value)


def _record_identity(data: Dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"Durable record field '{key}' must contain a stable non-empty identity.")
    return value


def _record_object(data: Dict[str, Any], key: str, *, required: bool = False) -> Dict[str, Any]:
    value = data.get(key) if required else data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"Durable record field '{key}' must be an object.")
    return dict(value)


def _unique_record_fields(records: List[Any], fields: tuple[str, ...]) -> None:
    seen = set()
    for record in records:
        key = tuple(getattr(record, name) for name in fields)
        if key in seen:
            raise ValueError("Configuration contains duplicate durable record identities; no records were discarded.")
        seen.add(key)


@dataclass
class PriceAlert:
    token_id: str
    label: str
    direction: Direction
    threshold: float
    source: PriceSource = "last_trade"
    once: bool = True
    enabled: bool = True
    market_id: str = DEFAULT_MARKET_ID

    id: str = field(default_factory=_uuid)
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_value: Optional[float] = None
    triggered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PriceAlert":
        data = dict(d)
        data["market_id"] = str(data.get("market_id") or DEFAULT_MARKET_ID).strip().lower()
        for key, default in (("enabled", True), ("once", True), ("triggered", False)):
            data[key] = _config_bool(data, key, default)
        return PriceAlert(**data)


@dataclass
class PaperTradeRecord:
    market_id: str
    contract_id: str
    side: str
    size: float
    limit_price: Optional[float]
    accepted: bool
    message: str
    filled_size: float = 0.0
    average_price: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    id: str = field(default_factory=_uuid)
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PaperTradeRecord":
        data = dict(d)
        data["market_id"] = str(data.get("market_id") or DEFAULT_MARKET_ID).strip().lower()
        data["contract_id"] = str(data.get("contract_id") or "")
        data["side"] = str(data.get("side") or "").upper()
        data["size"] = float(data.get("size") or 0.0)
        raw_limit = data.get("limit_price")
        data["limit_price"] = None if raw_limit in (None, "") else float(raw_limit)
        data["accepted"] = _config_bool(data, "accepted", False)
        data["message"] = str(data.get("message") or "")
        data["filled_size"] = float(data.get("filled_size") or 0.0)
        raw_average = data.get("average_price")
        data["average_price"] = None if raw_average in (None, "") else float(raw_average)
        raw = data.get("raw")
        data["raw"] = dict(raw) if isinstance(raw, dict) else {}
        return PaperTradeRecord(**data)


@dataclass
class WalletWatch:
    """Tracks a wallet/proxyWallet and optionally enables copy-trading."""
    wallet: str
    display_name: str = ""
    enabled: bool = True
    id: str = field(default_factory=_uuid)

    # tracking state
    last_seen_ts: int = 0
    last_seen_tx: str = ""
    seen_activity_keys: List[str] = field(default_factory=list)

    # optional filters
    only_market_slug: str = ""  # if set, only emit events for this market slug

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "WalletWatch":
        data = dict(d)
        data["enabled"] = _config_bool(data, "enabled", True)
        return WalletWatch(**data)


@dataclass
class CopyActivityOutboxEntry:
    """Durable copy-trading disposition for one observed wallet activity.

    ``pending`` and ``retryable`` entries may be processed automatically.
    ``ambiguous`` means a live dispatch may have reached the venue and must
    never be retried without operator reconciliation.
    """

    watch_id: str
    activity_key: str
    activity: Dict[str, Any]
    market_id: str = DEFAULT_MARKET_ID
    execution_policy: Dict[str, Any] = field(default_factory=dict)
    state: CopyActivityState = "pending"
    attempts: int = 0
    outcome_code: str = ""
    outcome_message: str = ""
    dispatch: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=_uuid)
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    replay_authorized_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CopyActivityOutboxEntry":
        data = dict(d or {})
        record_id = _record_identity(data, "id")
        watch_id = _record_identity(data, "watch_id")
        activity_key = _record_identity(data, "activity_key")
        market_id = _record_identity(data, "market_id").lower()
        raw_state = str(data.get("state") or "ambiguous").strip().lower()
        # Unknown future/invalid states fail closed: automatic replay could
        # duplicate a live order whose dispatch status is not understood.
        state: CopyActivityState = (
            cast(CopyActivityState, raw_state)
            if raw_state in {"pending", "retryable", "completed", "rejected", "ambiguous"}
            else "ambiguous"
        )
        activity = _record_object(data, "activity", required=True)
        dispatch = _record_object(data, "dispatch")
        execution_policy = _record_object(data, "execution_policy")
        if execution_policy:
            CopyTradeSettings.from_dict(execution_policy)
        attempts = _config_integer(data.get("attempts", 0), "attempts")
        created_at = _config_integer(data.get("created_at", 0), "created_at")
        updated_at = _config_integer(data.get("updated_at", created_at), "updated_at")
        replay_authorized_at = _config_integer(data.get("replay_authorized_at", 0), "replay_authorized_at")
        return CopyActivityOutboxEntry(
            watch_id=watch_id,
            activity_key=activity_key,
            activity=activity,
            market_id=market_id,
            execution_policy=execution_policy,
            state=state,
            attempts=attempts,
            outcome_code=str(data.get("outcome_code") or ""),
            outcome_message=str(data.get("outcome_message") or ""),
            dispatch=dispatch,
            id=record_id,
            created_at=created_at,
            updated_at=updated_at,
            replay_authorized_at=replay_authorized_at,
        )


def _shape_trim_mutation_value(value: Any, *, list_limit: int, string_limit: int) -> Any:
    """Trim bulky result fields while retaining their JSON/container shape."""

    if isinstance(value, dict):
        return {
            str(key): _shape_trim_mutation_value(
                child,
                list_limit=list_limit,
                string_limit=string_limit,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _shape_trim_mutation_value(
                child,
                list_limit=list_limit,
                string_limit=string_limit,
            )
            for child in value[: max(0, list_limit)]
        ]
    if isinstance(value, str) and len(value) > string_limit:
        return value[: max(0, string_limit)]
    return value


def bounded_mutation_result(
    value: Any,
    *,
    preserve_shape: bool = False,
    max_bytes: int = MAX_MUTATION_RESULT_BYTES,
) -> Dict[str, Any]:
    """Return one JSON-safe, size-bounded durable mutation result.

    The journal is part of the persisted configuration, so it must not grow
    with arbitrary venue responses.  Oversized results are replaced with a
    stable receipt that is safe to replay without re-running the mutation.
    """

    candidate = dict(value) if isinstance(value, dict) else {"result": value}
    try:
        encoded = json.dumps(
            candidate,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return {
            "ok": True,
            "mutation_result": {
                "stored": "receipt_only",
                "reason": "result_was_not_json_serializable",
            },
        }
    limit = max(1, int(max_bytes))
    if len(encoded) <= limit:
        return json.loads(encoded.decode("utf-8"))
    if preserve_shape:
        # Prefer a compact, schema-shaped replay over a receipt-only object for
        # routes whose clients dereference nested fields (for example
        # ``paper`` and ``result.message``).  The final receipt fallback keeps
        # the journal bounded even for unusually wide or deeply nested input.
        for list_limit, string_limit in (
            (128, 8 * 1024),
            (64, 4 * 1024),
            (32, 2 * 1024),
            (16, 1024),
            (8, 512),
            (4, 256),
            (0, 128),
        ):
            trimmed = _shape_trim_mutation_value(
                candidate,
                list_limit=list_limit,
                string_limit=string_limit,
            )
            try:
                trimmed_encoded = json.dumps(
                    trimmed,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError):
                continue
            if len(trimmed_encoded) <= limit:
                return json.loads(trimmed_encoded.decode("utf-8"))
    return {
        "ok": True,
        "mutation_result": {
            "stored": "receipt_only",
            "reason": "result_exceeded_storage_limit",
            "original_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        },
    }


@dataclass
class MutationJournalEntry:
    """Durable idempotency disposition for one web mutation.

    Only hashes of the client key and canonical request are retained.  A
    ``pending`` or unknown live state is treated as ambiguous on replay,
    preventing an automatic second venue dispatch after a crash or timeout.
    """

    key_hash: str
    method: str
    path: str
    request_hash: str
    live: bool = False
    state: MutationJournalState = "pending"
    response_status: int = 0
    response: Dict[str, Any] = field(default_factory=dict)
    outcome_code: str = ""
    outcome_message: str = ""
    id: str = field(default_factory=_uuid)
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    replay_authorized_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        _record_object(data, "response", required=True)
        data["response"] = bounded_mutation_result(self.response)
        return data

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MutationJournalEntry":
        data = dict(d or {})
        record_id = _record_identity(data, "id")
        key_hash = _record_identity(data, "key_hash")
        request_hash = _record_identity(data, "request_hash")
        for digest in (key_hash, request_hash):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("Mutation journal hashes must be canonical SHA-256 digests.")
        method = _record_identity(data, "method")
        path = _record_identity(data, "path")
        if method not in {"POST", "PATCH", "PUT", "DELETE"} or not path.startswith("/api/") or "?" in path or "#" in path:
            raise ValueError("Mutation journal request identity is invalid.")
        if "live" not in data:
            raise ValueError("Mutation journal execution classification is missing.")
        live = _config_bool(data, "live", False)
        raw_state = str(data.get("state") or "ambiguous").strip().lower()
        state: MutationJournalState = (
            cast(MutationJournalState, raw_state)
            if raw_state in {"pending", "retryable", "completed", "rejected", "ambiguous"}
            else "ambiguous"
        )
        response_status = _config_integer(data.get("response_status", 0), "response_status", 599)
        if (response_status and response_status < 100) or (state == "completed" and response_status == 0):
            raise ValueError("Mutation journal response status is invalid.")
        response = _record_object(data, "response")
        created_at = _config_integer(data.get("created_at", 0), "created_at")
        updated_at = _config_integer(data.get("updated_at", created_at), "updated_at")
        replay_authorized_at = _config_integer(data.get("replay_authorized_at", 0), "replay_authorized_at")
        return MutationJournalEntry(
            key_hash=key_hash,
            method=method,
            path=path,
            request_hash=request_hash,
            live=live,
            state=state,
            response_status=response_status,
            response=bounded_mutation_result(response),
            outcome_code=str(data.get("outcome_code") or ""),
            outcome_message=str(data.get("outcome_message") or ""),
            id=record_id,
            created_at=created_at,
            updated_at=updated_at,
            replay_authorized_at=replay_authorized_at,
        )


@dataclass
class CopyTradeSettings:
    """Risk controls for copy trading."""
    enabled: bool = False
    live: bool = False  # False = paper/sim
    follow_wallet: str = ""  # wallet address to follow
    follow_wallets: List[str] = field(default_factory=list)
    scale: float = 1.0  # 0..1 multiplier derived from copy_percentage
    max_usdc_per_trade: float = 25.0
    slippage: float = 0.02  # in price units (0..1)
    allow_sells: bool = False
    conflict_guard: bool = True
    conflict_window_seconds: int = 300

    @staticmethod
    def _stored_identity(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Copy follow identities must be strings.")
        text = value.strip()
        if text.lower().startswith("solana:"):
            return "solana:" + text.split(":", 1)[1].strip()
        return text.lower()

    def to_dict(self) -> Dict[str, Any]:
        data = self._validated_risk(asdict(self))
        data["follow_wallets"] = self.normalized_follow_wallets()
        data["follow_wallet"] = data["follow_wallets"][0] if data["follow_wallets"] else ""
        data["copy_percentage"] = round(data["scale"] * 100.0, 10)
        return data

    def normalized_follow_wallets(self) -> List[str]:
        if not isinstance(self.follow_wallets, list):
            raise ValueError("Copy follow_wallets must be a list.")
        wallets: List[str] = []
        for value in [self.follow_wallet, *self.follow_wallets]:
            wallet = self._stored_identity(value)
            if wallet and wallet not in wallets:
                wallets.append(wallet)
        return wallets

    @staticmethod
    def _validated_risk(d: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(d)
        for key, default in (("enabled", False), ("live", False), ("allow_sells", False), ("conflict_guard", True)):
            data[key] = _config_bool(data, key, default)
        scale = _config_number(data.get("scale", 1.0), "scale", 0, 1)
        if "copy_percentage" in data:
            percentage_scale = _config_number(data.pop("copy_percentage"), "copy_percentage", 0, 100) / 100
            # to_dict rounds the UI percentage to ten decimal places.
            if "scale" in data and not math.isclose(scale, percentage_scale, rel_tol=0, abs_tol=1e-12):
                raise ValueError("Copy percentage and scale disagree.")
            if "scale" not in data:
                scale = percentage_scale
        data["scale"] = scale
        data["max_usdc_per_trade"] = _config_number(data.get("max_usdc_per_trade", 25.0), "max_usdc_per_trade", 0, positive=True)
        data["slippage"] = _config_number(data.get("slippage", 0.02), "slippage", 0, 1)
        data["conflict_window_seconds"] = _config_integer(data.get("conflict_window_seconds", 300), "conflict_window_seconds", 86400)
        return data

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CopyTradeSettings":
        data = CopyTradeSettings._validated_risk(d)
        raw_wallets = data.get("follow_wallets", [])
        if isinstance(raw_wallets, str):
            raw_wallets = raw_wallets.replace(";", ",").split(",")
        if not isinstance(raw_wallets, list):
            raise ValueError("Copy follow_wallets must be a list or delimited string.")
        wallets: List[str] = []
        for value in [data.get("follow_wallet", ""), *raw_wallets]:
            wallet = CopyTradeSettings._stored_identity(value)
            if wallet and wallet not in wallets:
                wallets.append(wallet)
        data["follow_wallet"] = wallets[0] if wallets else ""
        data["follow_wallets"] = wallets
        return CopyTradeSettings(**data)


@dataclass
class MarketConfig:
    market_id: str
    enabled: bool = False
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "settings": dict(self.settings),
        }

    @staticmethod
    def from_dict(market_id: str, d: Dict[str, Any]) -> "MarketConfig":
        settings = d.get("settings", {})
        if not isinstance(settings, dict):
            settings = {}
        return MarketConfig(
            market_id=str(d.get("market_id") or market_id),
            enabled=_config_bool(d, "enabled", False),
            settings=dict(settings),
        )


def default_market_configs() -> Dict[str, MarketConfig]:
    return {
        meta.market_id: MarketConfig(market_id=meta.market_id, enabled=meta.default_enabled)
        for meta in MARKET_CATALOG
    }


def _config_records(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    records = data.get(key, [])
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise ValueError(f"Configuration field '{key}' must be a list of objects; no records were discarded.")
    return records


@dataclass
class AppConfig:
    alerts: List[PriceAlert] = field(default_factory=list)
    paper_trades: List[PaperTradeRecord] = field(default_factory=list)
    wallets: List[WalletWatch] = field(default_factory=list)
    copy_activity_outbox: List[CopyActivityOutboxEntry] = field(default_factory=list)
    mutation_journal: List[MutationJournalEntry] = field(default_factory=list)
    copytrading: CopyTradeSettings = field(default_factory=CopyTradeSettings)
    markets: Dict[str, MarketConfig] = field(default_factory=default_market_configs)
    selected_market_id: str = DEFAULT_MARKET_ID
    theme: Theme = "light"
    ui_design: UIDesign = DEFAULT_UI_DESIGN

    def reconcile_ambiguous_copy_activity(
        self,
        entry_id: str,
        resolution: str,
    ) -> CopyActivityOutboxEntry:
        """Record an operator's venue reconciliation for one ambiguous dispatch."""

        entry = next((item for item in self.copy_activity_outbox if item.id == entry_id), None)
        if entry is None:
            raise ValueError("Copy activity outbox entry was not found.")
        if entry.state != "ambiguous":
            raise ValueError("Only ambiguous copy activity entries can be reconciled.")
        normalized = str(resolution or "").strip().lower().replace("-", "_")
        transitions = {
            "confirmed_dispatched": (
                "completed",
                "manual_dispatch_confirmed",
                "Operator confirmed the live dispatch in venue order history.",
            ),
            "confirmed_not_dispatched": (
                "retryable",
                "manual_dispatch_cleared",
                "Operator confirmed no venue dispatch; automatic retry is allowed.",
            ),
            "discard": (
                "rejected",
                "manual_dispatch_discarded",
                "Operator discarded the ambiguous activity without retry.",
            ),
        }
        transition = transitions.get(normalized)
        if transition is None:
            raise ValueError(
                "Resolution must be confirmed_dispatched, confirmed_not_dispatched, or discard."
            )
        state, entry.outcome_code, entry.outcome_message = transition
        entry.state = cast(CopyActivityState, state)
        entry.updated_at = int(time.time())
        entry.replay_authorized_at = entry.updated_at if normalized == "confirmed_not_dispatched" else 0
        return entry

    def append_mutation_journal(self, entry: MutationJournalEntry) -> None:
        """Append an entry while preserving unresolved live safety records."""

        if len(self.mutation_journal) >= MAX_MUTATION_JOURNAL_ENTRIES:
            removable = sorted(
                (
                    item
                    for item in self.mutation_journal
                    if item.state in {"completed", "rejected"}
                ),
                key=lambda item: (item.updated_at, item.created_at, item.id),
            )
            if not removable:
                raise ValueError(
                    "The mutation journal is full of unresolved entries; reconcile them before submitting another durable mutation."
                )
            remove_id = removable[0].id
            self.mutation_journal = [item for item in self.mutation_journal if item.id != remove_id]
        self.mutation_journal.append(entry)

    def reconcile_ambiguous_mutation(
        self,
        entry_id: str,
        resolution: str,
        response: Optional[Dict[str, Any]] = None,
    ) -> MutationJournalEntry:
        """Record an operator's venue reconciliation for one live mutation."""

        entry = next((item for item in self.mutation_journal if item.id == entry_id), None)
        if entry is None:
            raise ValueError("Mutation journal entry was not found.")
        if not entry.live or entry.state not in {"pending", "ambiguous"}:
            raise ValueError("Only pending or ambiguous live mutations can be reconciled.")
        normalized = str(resolution or "").strip().lower().replace("-", "_")
        now = int(time.time())
        if normalized == "confirmed_dispatched":
            entry.state = "completed"
            entry.response_status = 200
            entry.response = bounded_mutation_result(
                response
                or {
                    "ok": True,
                    "mutation": {
                        "id": entry.id,
                        "state": "completed",
                        "reconciled": True,
                    },
                }
            )
            entry.outcome_code = "manual_dispatch_confirmed"
            entry.outcome_message = "Operator confirmed the live mutation in venue history."
            entry.replay_authorized_at = 0
        elif normalized == "confirmed_not_dispatched":
            entry.state = "retryable"
            entry.response_status = 0
            entry.response = {}
            entry.outcome_code = "manual_dispatch_cleared"
            entry.outcome_message = "Operator confirmed no venue dispatch; one retry is authorized."
            entry.replay_authorized_at = now
        elif normalized == "discard":
            entry.state = "rejected"
            entry.response_status = 409
            entry.response = {}
            entry.outcome_code = "manual_dispatch_discarded"
            entry.outcome_message = "Operator discarded the ambiguous mutation without retry."
            entry.replay_authorized_at = 0
        else:
            raise ValueError(
                "Resolution must be confirmed_dispatched, confirmed_not_dispatched, or discard."
            )
        entry.updated_at = now
        return entry

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alerts": [a.to_dict() for a in self.alerts],
            "paper_trades": [t.to_dict() for t in self.paper_trades],
            "wallets": [w.to_dict() for w in self.wallets],
            "copy_activity_outbox": [entry.to_dict() for entry in self.copy_activity_outbox],
            "mutation_journal": [entry.to_dict() for entry in self.mutation_journal],
            "copytrading": self.copytrading.to_dict(),
            "markets": {market_id: cfg.to_dict() for market_id, cfg in self.markets.items()},
            "selected_market_id": self.selected_market_id,
            "theme": self.theme,
            "ui_design": self.ui_design,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AppConfig":
        if not isinstance(d, dict):
            raise ValueError("Configuration must contain a JSON object.")
        alerts = [PriceAlert.from_dict(x) for x in _config_records(d, "alerts")]
        paper_trades = [PaperTradeRecord.from_dict(x) for x in _config_records(d, "paper_trades")]
        wallets = [WalletWatch.from_dict(x) for x in _config_records(d, "wallets")]
        copy_activity_outbox = [CopyActivityOutboxEntry.from_dict(x) for x in _config_records(d, "copy_activity_outbox")]
        raw_mutation_journal = _config_records(d, "mutation_journal")
        if len(raw_mutation_journal) > MAX_MUTATION_JOURNAL_ENTRIES:
            # Loading must never evict a pending live operation. Retention is
            # applied explicitly by append_mutation_journal, not by recovery.
            raise ValueError("Configuration mutation journal exceeds its supported capacity; no records were discarded.")
        mutation_journal = [MutationJournalEntry.from_dict(x) for x in raw_mutation_journal]
        _unique_record_fields(mutation_journal, ("id",))
        _unique_record_fields(mutation_journal, ("key_hash",))
        _unique_record_fields(copy_activity_outbox, ("id",))
        _unique_record_fields(copy_activity_outbox, ("market_id", "watch_id", "activity_key"))
        if not isinstance(d.get("copytrading", {}), dict):
            raise ValueError("Configuration copytrading settings must be an object.")
        copytrading = CopyTradeSettings.from_dict(d.get("copytrading", {}))
        markets = default_market_configs()
        raw_markets = d.get("markets", {})
        if not isinstance(raw_markets, dict):
            raise ValueError("Configuration markets must be an object.")
        for market_id, raw_cfg in raw_markets.items():
            if not isinstance(raw_cfg, dict) or not isinstance(raw_cfg.get("settings", {}), dict):
                raise ValueError("Configuration market and safety settings must be objects.")
            cfg = MarketConfig.from_dict(str(market_id), raw_cfg)
            markets[cfg.market_id] = cfg
        selected_market_id = str(d.get("selected_market_id") or DEFAULT_MARKET_ID).strip().lower()
        if selected_market_id not in markets:
            selected_market_id = DEFAULT_MARKET_ID
        raw_theme = str(d.get("theme") or "").lower()
        theme: Theme = "dark" if raw_theme == "dark" else "light"
        raw_ui_design = str(d.get("ui_design") or DEFAULT_UI_DESIGN).strip().lower().replace("-", "_")
        ui_design = (
            cast(UIDesign, raw_ui_design)
            if raw_ui_design in {"classic", "aurora_2026", "graphite_2026", "sentinel_2027"}
            else DEFAULT_UI_DESIGN
        )
        return AppConfig(
            alerts=alerts,
            paper_trades=paper_trades,
            wallets=wallets,
            copy_activity_outbox=copy_activity_outbox,
            mutation_journal=mutation_journal,
            copytrading=copytrading,
            markets=markets,
            selected_market_id=selected_market_id,
            theme=theme,
            ui_design=ui_design,
        )
