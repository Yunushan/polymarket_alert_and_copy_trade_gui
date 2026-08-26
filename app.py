from __future__ import annotations

import json
import importlib
import os
import queue
import re
import csv
import sys
import threading
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib import request as urllib_request
from urllib import error as urllib_error
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from dotenv import load_dotenv

from core.models import (
    AppConfig,
    CopyActivityOutboxEntry,
    CopyActivityState,
    CopyTradeSettings,
    MarketConfig,
    PaperTradeRecord,
    PriceAlert,
    WalletWatch,
)
from core.storage import ConfigLoadError, load_config, save_config
from market_adapters import build_default_registry
from market_adapters.base import MarketAdapter
from market_adapters.errors import MarketConfigurationError, UnsupportedFeatureError
from market_adapters.identity import activity_identity_hint, normalize_activity_identity
from market_adapters.polymarket import PolymarketAdapter
from market_adapters.types import (
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)

from polymarket.util import is_wallet_address, normalize_wallet
from polymarket import data_api
from polymarket.ws_market import MarketWSClient
from polymarket.trader import PolymarketTrader, TraderConfig


# ---------------------------
# Helpers
# ---------------------------

APP_ID = "market-sentinel"
APP_TITLE = "MarketSentinel"
APP_USER_AGENT = f"{APP_ID}/1.0"

DEPENDENCY_IMPORT_FALLBACKS = {
    "websocket-client": ("websocket",),
    "python-dotenv": ("dotenv",),
    "py-clob-client": ("py_clob_client",),
    "eth-account": ("eth_account",),
    "eth-abi": ("eth_abi",),
}


def extract_slug(s: str) -> str:
    """
    Accept a raw slug or a Polymarket URL and try to extract the last path segment.
    This is best-effort because Polymarket URLs can differ by page type.
    """
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"[?#].*$", "", s)  # drop query/fragment
    # If it's a URL, take last non-empty path segment
    if "://" in s:
        parts = [p for p in s.split("/") if p]
        return parts[-1] if parts else ""
    return s.strip("/")


def safe_float(s: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return default


def activity_key(item: Dict[str, Any]) -> str:
    """Stable local identity for a Data API activity item."""
    tx = str(item.get("transactionHash") or "").strip().lower()
    if tx:
        return f"tx:{tx}"
    activity_id = str(item.get("activityId") or item.get("activity_id") or "").strip().lower()
    if activity_id:
        return f"activity-id:{activity_id}"
    fields = ("timestamp", "proxyWallet", "asset", "side", "price", "size", "slug", "outcome")
    return "activity:" + "|".join(str(item.get(k) or "").strip().lower() for k in fields)


def market_choice_label(metadata: MarketMetadata) -> str:
    return f"{metadata.display_name} ({metadata.market_id})"


def market_id_from_choice(choice: str) -> str:
    match = re.search(r"\(([^()]+)\)\s*$", str(choice or ""))
    if match:
        return match.group(1).strip().lower()
    return str(choice or "").strip().lower()


UI_DESIGN_LABELS = {
    "classic": "Classic",
    "aurora_2026": "Aurora 2026",
    "graphite_2026": "Graphite 2026",
    "sentinel_2027": "Sentinel 2027",
}

UI_DESIGN_BY_LABEL = {label.lower(): key for key, label in UI_DESIGN_LABELS.items()}


def bool_from_setting(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def optional_positive_float(raw: Any, label: str) -> Optional[float]:
    text = str(raw or "").strip()
    if not text:
        return None
    value = safe_float(text, None)
    if value is None or value <= 0:
        raise ValueError(f"{label} must be blank or a positive number.")
    return float(value)


def market_config_enabled(cfg: AppConfig, market_id: str) -> bool:
    normalized = str(market_id or "polymarket").strip().lower()
    market_cfg = cfg.markets.get(normalized)
    return bool(market_cfg and market_cfg.enabled)


def set_windows_app_id(app_id: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def set_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        awareness_context_per_monitor_v2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(awareness_context_per_monitor_v2):
            return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _hex_to_colorref(color: str, fallback: str) -> int:
    text = str(color or fallback).strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(part * 2 for part in text)
    if len(text) != 6:
        text = fallback.lstrip("#")
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except Exception:
        red = int(fallback[1:3], 16)
        green = int(fallback[3:5], 16)
        blue = int(fallback[5:7], 16)
    return red | (green << 8) | (blue << 16)


def _set_windows_titlebar_theme(
    window: tk.Misc,
    *,
    dark: bool,
    caption_color: str,
    text_color: str,
    border_color: str,
) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        base_hwnd = int(window.winfo_id())
        if not base_hwnd:
            return

        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        hwnds = [base_hwnd]
        for candidate in (
            user32.GetParent(ctypes.c_void_p(base_hwnd)),
            user32.GetAncestor(ctypes.c_void_p(base_hwnd), ctypes.c_uint(2)),
        ):
            candidate = int(candidate or 0)
            if candidate and candidate not in hwnds:
                hwnds.append(candidate)

        for hwnd in hwnds:
            enabled = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )

            for attribute, color, fallback in (
                (34, border_color, "#2a3a4a" if dark else "#c9c3b8"),
                (35, caption_color, "#151d27" if dark else "#f6f4ef"),
                (36, text_color, "#ffffff" if dark else "#161616"),
            ):
                colorref = ctypes.c_int(_hex_to_colorref(color, fallback))
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(colorref),
                    ctypes.sizeof(colorref),
                )
    except Exception:
        pass


# ---------------------------
# Background wallet poller
# ---------------------------

@dataclass
class WalletActivityTask:
    watch_id: str
    activity: Dict[str, Any]
    checkpoint_key: str
    market_id: str = ""
    completion: "queue.Queue[bool]" = field(default_factory=lambda: queue.Queue(maxsize=1))

    def acknowledge(self, persisted: bool) -> None:
        try:
            self.completion.put_nowait(bool(persisted))
        except queue.Full:
            pass


@dataclass(frozen=True)
class CopyActivityOutcome:
    state: CopyActivityState
    code: str
    message: str
    dispatch: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WalletActivityCheckpoint:
    cfg: AppConfig
    watch: WalletWatch
    previous_watch: Tuple[Optional[int], str, List[str]]
    previous_outbox: List[CopyActivityOutboxEntry]
    changed: bool = False


_COPY_ACTIVITY_FIELDS = (
    "timestamp",
    "transactionHash",
    "activityId",
    "activity_id",
    "proxyWallet",
    "asset",
    "contract_id",
    "side",
    "price",
    "size",
    "slug",
    "outcome",
    "odds",
    "shares",
    "pseudonym",
    "name",
)
_COPY_OUTBOX_CONCLUSIVE_STATES = {"completed", "rejected"}
_COPY_OUTBOX_REPLAYABLE_STATES = {"pending", "retryable"}
_COPY_OUTBOX_HISTORY_LIMIT = 500
_COPY_ACTIVITY_MAX_REPLAY_AGE_SECONDS = 300


def _copy_activity_snapshot(activity: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the scalar fields needed to audit and replay copy handling."""

    snapshot: Dict[str, Any] = {}
    for key in _COPY_ACTIVITY_FIELDS:
        value = activity.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            snapshot[key] = value
    return snapshot


def _copy_execution_policy(settings: CopyTradeSettings) -> Dict[str, Any]:
    """Canonical policy bound to a signal before any processing occurs."""

    return {
        "allow_sells": bool(settings.allow_sells),
        "conflict_guard": bool(settings.conflict_guard),
        "conflict_window_seconds": max(0, int(settings.conflict_window_seconds or 0)),
        "enabled": bool(settings.enabled),
        "follow_wallets": list(settings.normalized_follow_wallets()),
        "live": bool(settings.live),
        "max_usdc_per_trade": float(settings.max_usdc_per_trade),
        "scale": float(settings.scale),
        "slippage": float(settings.slippage),
    }


def _find_copy_activity_entry(
    cfg: AppConfig,
    watch_id: str,
    checkpoint_key: str,
    market_id: str = "",
) -> Optional[CopyActivityOutboxEntry]:
    normalized_market_id = str(market_id or "").strip().lower()
    return next(
        (
            entry
            for entry in cfg.copy_activity_outbox
            if entry.watch_id == watch_id and entry.activity_key == checkpoint_key
            and (not normalized_market_id or entry.market_id == normalized_market_id)
        ),
        None,
    )


def _prune_copy_activity_outbox(cfg: AppConfig) -> None:
    overflow = len(cfg.copy_activity_outbox) - _COPY_OUTBOX_HISTORY_LIMIT
    if overflow <= 0:
        return
    removable_ids = {
        entry.id
        for entry in sorted(cfg.copy_activity_outbox, key=lambda item: (item.updated_at, item.created_at))
        if entry.state in _COPY_OUTBOX_CONCLUSIVE_STATES
    }
    retained: List[CopyActivityOutboxEntry] = []
    for entry in cfg.copy_activity_outbox:
        if overflow > 0 and entry.id in removable_ids:
            overflow -= 1
            removable_ids.remove(entry.id)
            continue
        retained.append(entry)
    cfg.copy_activity_outbox = retained


def _apply_wallet_activity_checkpoint(
    cfg: AppConfig,
    task: WalletActivityTask,
) -> Optional[WalletActivityCheckpoint]:
    watch = next((item for item in cfg.wallets if item.id == task.watch_id), None)
    if watch is None:
        raise ValueError("Wallet watch no longer exists.")
    source_market_id = str(task.market_id or cfg.selected_market_id or "polymarket").strip().lower()
    existing = _find_copy_activity_entry(
        cfg,
        task.watch_id,
        task.checkpoint_key,
        source_market_id,
    )
    if existing is not None and existing.state not in _COPY_OUTBOX_REPLAYABLE_STATES:
        return None
    any_outbox_match = _find_copy_activity_entry(cfg, task.watch_id, task.checkpoint_key)
    if (
        existing is None
        and any_outbox_match is None
        and task.checkpoint_key in set(watch.seen_activity_keys or [])
    ):
        # Legacy checkpoints did not include outbox state. Preserve their
        # historical at-most-once behavior instead of guessing whether a live
        # order was placed.
        return None
    previous = (watch.last_seen_ts, watch.last_seen_tx, list(watch.seen_activity_keys))
    checkpoint = WalletActivityCheckpoint(cfg, watch, previous, list(cfg.copy_activity_outbox))
    if existing is None:
        if len(cfg.copy_activity_outbox) >= _COPY_OUTBOX_HISTORY_LIMIT:
            required_slots = len(cfg.copy_activity_outbox) - _COPY_OUTBOX_HISTORY_LIMIT + 1
            oldest_conclusive = sorted(
                (
                    entry
                    for entry in cfg.copy_activity_outbox
                    if entry.state in _COPY_OUTBOX_CONCLUSIVE_STATES
                ),
                key=lambda entry: (entry.updated_at, entry.created_at, entry.id),
            )
            if len(oldest_conclusive) < required_slots:
                raise RuntimeError(
                    "Copy activity outbox is full of unresolved entries; reconcile them before polling."
                )
            removable_ids = {entry.id for entry in oldest_conclusive[:required_slots]}
            cfg.copy_activity_outbox = [
                entry for entry in cfg.copy_activity_outbox if entry.id not in removable_ids
            ]
        watch.last_seen_ts = max(watch.last_seen_ts or 0, int(task.activity.get("timestamp") or 0))
        watch.last_seen_tx = str(task.activity.get("transactionHash") or watch.last_seen_tx or "")
        watch.seen_activity_keys.append(task.checkpoint_key)
        if len(watch.seen_activity_keys) > 200:
            watch.seen_activity_keys = watch.seen_activity_keys[-200:]
        cfg.copy_activity_outbox.append(
            CopyActivityOutboxEntry(
                watch_id=task.watch_id,
                activity_key=task.checkpoint_key,
                activity=_copy_activity_snapshot(task.activity),
                market_id=source_market_id,
                execution_policy=_copy_execution_policy(cfg.copytrading),
            )
        )
        checkpoint.changed = True
        _prune_copy_activity_outbox(cfg)
    return checkpoint


def _restore_wallet_activity_checkpoint(
    checkpoint: WalletActivityCheckpoint,
) -> None:
    watch = checkpoint.watch
    watch.last_seen_ts, watch.last_seen_tx, previous_keys = checkpoint.previous_watch
    watch.seen_activity_keys = previous_keys
    checkpoint.cfg.copy_activity_outbox = checkpoint.previous_outbox


def _set_copy_activity_outcome(
    entry: CopyActivityOutboxEntry,
    outcome: CopyActivityOutcome,
) -> None:
    if outcome.state not in {"retryable", "completed", "rejected", "ambiguous"}:
        raise ValueError(f"Unsupported copy activity outcome state: {outcome.state}")
    entry.state = outcome.state
    entry.outcome_code = outcome.code
    entry.outcome_message = outcome.message
    entry.dispatch = dict(outcome.dispatch)
    entry.updated_at = int(time.time())


class WalletPoller:
    def __init__(
        self,
        ui_queue: "queue.Queue[tuple]",
        cfg: AppConfig,
        poll_interval: float = 10.0,
        adapter_registry: Any = None,
    ):
        self.ui_queue = ui_queue
        self.cfg = cfg
        self.poll_interval = poll_interval
        self.adapter_registry = adapter_registry
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _list_activity(self, wallet: str, *, market_id: str = "") -> List[Dict[str, Any]]:
        """Read wallet trades through the selected market's official adapter feed."""

        market_id = str(
            market_id or getattr(self.cfg, "selected_market_id", "polymarket") or "polymarket"
        ).strip().lower()
        market_cfg = self.cfg.markets.get(market_id)
        if self.adapter_registry is not None:
            adapter = self.adapter_registry.create(market_id, market_cfg.settings if market_cfg else {})
            list_activity = getattr(adapter, "list_activity", None)
            if callable(list_activity):
                items = list_activity(wallet, limit=25)
                return list(items or [])
        if market_id == "polymarket":
            return list(data_api.get_activity(wallet, limit=25, types=["TRADE"]) or [])
        raise UnsupportedFeatureError(
            market_id,
            "copy_trading",
            "This market does not expose an official wallet activity feed in the desktop app.",
        )

    def _queue_wallet_activity(self, task: WalletActivityTask) -> bool:
        self.ui_queue.put(("wallet_activity", task))
        while not self._stop.is_set():
            try:
                return bool(task.completion.get(timeout=0.25))
            except queue.Empty:
                continue
        return False

    def _run(self):
        startup_pending_ids = {
            entry.id for entry in self.cfg.copy_activity_outbox if entry.state == "pending"
        }
        for entry in self.cfg.copy_activity_outbox:
            if entry.state == "ambiguous":
                self.ui_queue.put(
                    (
                        "log",
                        "[copy reconcile] Activity "
                        f"{entry.activity_key} may have dispatched a live order. "
                        "Automatic replay is blocked; reconcile it against venue order history.",
                    )
                )
        while not self._stop.is_set():
            for w in list(self.cfg.wallets):
                if self._stop.is_set():
                    break
                if not w.enabled:
                    continue
                try:
                    replay_entries = sorted(
                        (
                            entry
                            for entry in self.cfg.copy_activity_outbox
                            if entry.watch_id == w.id
                            and (
                                entry.state == "retryable"
                                or (entry.state == "pending" and entry.id in startup_pending_ids)
                            )
                        ),
                        key=lambda entry: (entry.created_at, entry.id),
                    )
                    replay_failed = False
                    for entry in replay_entries:
                        startup_pending_ids.discard(entry.id)
                        task = WalletActivityTask(
                            w.id,
                            dict(entry.activity),
                            entry.activity_key,
                            entry.market_id,
                        )
                        if not self._queue_wallet_activity(task):
                            replay_failed = True
                            break
                    if replay_failed or self._stop.is_set():
                        continue

                    source_market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
                    items = self._list_activity(w.wallet, market_id=source_market_id)
                    # Items are sorted DESC per API; process oldest->newest
                    new_items = []
                    seen_keys = set(w.seen_activity_keys or [])
                    for it in reversed(items):
                        ts = int(it.get("timestamp") or 0)
                        tx = str(it.get("transactionHash") or "")
                        key = activity_key(it)
                        if key in seen_keys:
                            same_market_entry = _find_copy_activity_entry(
                                self.cfg,
                                w.id,
                                key,
                                source_market_id,
                            )
                            any_market_entry = _find_copy_activity_entry(self.cfg, w.id, key)
                            if same_market_entry is not None or any_market_entry is None:
                                continue
                            # A global legacy key collided with an outbox entry
                            # from another venue. Preserve the market-bound
                            # signal instead of silently dropping it.
                            new_items.append((key, it))
                            continue
                        if ts > (w.last_seen_ts or 0):
                            new_items.append((key, it))
                            seen_keys.add(key)
                        elif ts == (w.last_seen_ts or 0) and (not tx or tx != (w.last_seen_tx or "")):
                            # Same timestamp but different activity; still emit once.
                            new_items.append((key, it))
                            seen_keys.add(key)

                    for key, it in new_items:
                        # market slug filter (optional)
                        if w.only_market_slug:
                            if str(it.get("slug") or "") != w.only_market_slug:
                                continue
                        # Queue one activity at a time and wait until the UI
                        # thread has durably checkpointed exactly this item.
                        # This prevents a later item in the same API batch from
                        # being persisted before an earlier item is handled.
                        task = WalletActivityTask(
                            w.id,
                            dict(it),
                            key,
                            source_market_id,
                        )
                        acknowledged = self._queue_wallet_activity(task)
                        if not acknowledged:
                            break
                        seen_keys.add(key)

                except Exception as e:
                    self.ui_queue.put(("log", f"[wallet poll] {w.wallet}: {e}"))

            # sleep
            end = time.time() + self.poll_interval
            while time.time() < end:
                if self._stop.is_set():
                    break
                time.sleep(0.2)


class AdapterPricePoller:
    def __init__(
        self,
        ui_queue: "queue.Queue[tuple]",
        cfg: AppConfig,
        adapter_registry: Any,
        poll_interval: float = 30.0,
        stream_connected: Optional[Callable[[str], bool]] = None,
    ):
        self.ui_queue = ui_queue
        self.cfg = cfg
        self.adapter_registry = adapter_registry
        self.poll_interval = poll_interval
        self.stream_connected = stream_connected or (lambda _market_id: False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def poll_once(self):
        grouped: Dict[str, set[str]] = {}
        for alert in list(self.cfg.alerts):
            if not alert.enabled:
                continue
            market_id = str(getattr(alert, "market_id", "polymarket") or "polymarket").strip().lower()
            grouped.setdefault(market_id, set()).add(alert.token_id)

        for market_id, contract_ids in grouped.items():
            if self.stream_connected(market_id):
                continue
            market_cfg = self.cfg.markets.get(market_id)
            if not market_config_enabled(self.cfg, market_id):
                self.ui_queue.put(("log", f"[alerts] {market_id}: disabled in local market config."))
                continue
            settings = market_cfg.settings if market_cfg else {}
            try:
                adapter = self.adapter_registry.create(market_id, settings)
            except Exception as exc:
                self.ui_queue.put(("log", f"[alerts] {market_id}: adapter unavailable: {exc}"))
                continue

            if not adapter.capabilities.price_reading:
                self.ui_queue.put(("log", f"[alerts] {adapter.display_name}: price alerts are not supported."))
                continue

            for contract_id in sorted(contract_ids):
                if self._stop.is_set():
                    return
                try:
                    snapshot = adapter.get_price(contract_id)
                    self.ui_queue.put(
                        (
                            "adapter_price",
                            {
                                "market_id": market_id,
                                "contract_id": contract_id,
                                "values": self._snapshot_values(snapshot),
                                "source": snapshot.source,
                            },
                            None,
                        )
                    )
                except UnsupportedFeatureError as exc:
                    self.ui_queue.put(("log", f"[alerts] {adapter.display_name}: {exc}"))
                except Exception as exc:
                    self.ui_queue.put(("log", f"[alerts] {adapter.display_name} {contract_id}: {exc}"))

    def _run(self):
        while not self._stop.is_set():
            self.poll_once()
            end = time.time() + self.poll_interval
            while time.time() < end:
                if self._stop.is_set():
                    break
                time.sleep(0.2)

    @staticmethod
    def _snapshot_values(snapshot: PriceSnapshot) -> Dict[str, Optional[float]]:
        bid = snapshot.bid
        ask = snapshot.ask
        midpoint = snapshot.midpoint
        if midpoint is None and bid is not None and ask is not None:
            midpoint = (bid + ask) / 2.0
        return {
            "last_trade": snapshot.last,
            "midpoint": midpoint,
            "best_bid": bid,
            "best_ask": ask,
        }


# ---------------------------
# Main GUI App
# ---------------------------

class App(tk.Tk):
    def __init__(self, *, start_background_workers: bool = True):
        set_windows_app_id(APP_ID)
        set_windows_dpi_awareness()
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1320x860")
        self.minsize(1180, 760)
        self._icon_images: List[tk.PhotoImage] = []
        self._load_icon_image()
        self._apply_window_icon(self)

        load_dotenv()

        self.cfg: AppConfig = load_config()
        self.cfg.theme = self._normalize_theme(self.cfg.theme)
        self.cfg.ui_design = self._normalize_ui_design(getattr(self.cfg, "ui_design", "aurora_2026"))
        self.adapter_registry = build_default_registry()
        self.polymarket_adapter = self._create_polymarket_adapter()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.style = ttk.Style(self)
        self._themes = self._build_theme_palettes()
        self._palette = self._palette_for(self.cfg.theme, self.cfg.ui_design)
        self._requirements = self._load_requirements()
        self._dep_check_running = False
        self._leaderboard_loading = False
        self._leaderboard_cancel_event = threading.Event()
        self._last_leaderboard_payload: Optional[Dict[str, Any]] = None
        self._background_workers_started = bool(start_background_workers)
        self._shutdown_started = False
        self._queue_after_id: Optional[str] = None

        # price state by token_id
        self.price_state: Dict[str, Dict[str, Optional[float]]] = {}
        self._paper_position_marks: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # WebSocket client (market channel)
        self.market_ws = MarketWSClient(
            token_ids=self._enabled_polymarket_alert_ids(),
            on_event=self._on_market_event_bg,
            custom_feature_enabled=False,
            verbose=False,
        )
        if self._background_workers_started:
            self.market_ws.start()

        # Wallet poller
        self.wallet_poller = WalletPoller(
            self.ui_queue,
            self.cfg,
            poll_interval=10.0,
            adapter_registry=self.adapter_registry,
        )
        self.adapter_price_poller = AdapterPricePoller(
            self.ui_queue,
            self.cfg,
            self.adapter_registry,
            stream_connected=lambda market_id: market_id == "polymarket" and self.market_ws.is_connected,
        )
        if self._background_workers_started:
            self.adapter_price_poller.start()

        # Cached trader
        self._trader: Optional[PolymarketTrader] = None
        self._geoblock_cache: Optional[Dict[str, Any]] = None
        self._copy_conflict_cache: Dict[str, Dict[str, Any]] = {}

        # UI
        self._build_ui()
        self._apply_theme(self.cfg.theme, self.cfg.ui_design)
        self._apply_window_icon(self)
        self.bind("<Map>", lambda _event: self._apply_native_titlebar(self), add="+")
        self.bind("<FocusIn>", lambda _event: self._apply_native_titlebar(self), add="+")

        # Kick off queue processing
        self._queue_after_id = self.after(100, self._process_queue)
        self.protocol("WM_DELETE_WINDOW", self.shutdown)

    def shutdown(self) -> None:
        """Stop background activity before destroying the Tk interpreter."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._leaderboard_cancel_event.set()
        for worker in (self.market_ws, self.wallet_poller, self.adapter_price_poller):
            try:
                worker.stop()
            except Exception:
                pass
        if self._queue_after_id:
            try:
                self.after_cancel(self._queue_after_id)
            except tk.TclError:
                pass
            self._queue_after_id = None
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ------------------ UI build ------------------

    def _create_polymarket_adapter(self) -> PolymarketAdapter:
        market_cfg = self.cfg.markets.get("polymarket")
        return PolymarketAdapter(market_cfg.settings if market_cfg else {})

    def _get_polymarket_adapter(self) -> PolymarketAdapter:
        adapter = getattr(self, "polymarket_adapter", None)
        if adapter is None:
            adapter = self._create_polymarket_adapter()
            self.polymarket_adapter = adapter
        return adapter

    @staticmethod
    def _alert_market_id(alert: PriceAlert) -> str:
        return str(getattr(alert, "market_id", "polymarket") or "polymarket").strip().lower()

    @staticmethod
    def _price_state_key(market_id: str, contract_id: str) -> str:
        normalized = str(market_id or "polymarket").strip().lower()
        return str(contract_id) if normalized == "polymarket" else f"{normalized}:{contract_id}"

    def _enabled_polymarket_alert_ids(self) -> List[str]:
        return [
            a.token_id
            for a in self.cfg.alerts
            if a.enabled and self._alert_market_id(a) == "polymarket"
        ]

    def _market_choices(self) -> List[str]:
        return [market_choice_label(meta) for meta in self.adapter_registry.list_metadata()]

    def _market_label_for_id(self, market_id: str) -> str:
        try:
            return market_choice_label(self.adapter_registry.get_metadata(market_id))
        except Exception:
            return market_choice_label(self.adapter_registry.get_metadata("polymarket"))

    def _get_selected_market_adapter(self) -> MarketAdapter:
        market_id = self.cfg.selected_market_id
        market_cfg = self.cfg.markets.get(market_id)
        settings = market_cfg.settings if market_cfg else {}
        return self.adapter_registry.create(market_id, settings)

    def _market_config_for(self, market_id: str) -> MarketConfig:
        normalized = str(market_id or "polymarket").strip().lower()
        market_cfg = self.cfg.markets.get(normalized)
        if market_cfg is None:
            market_cfg = MarketConfig(market_id=normalized)
            self.cfg.markets[normalized] = market_cfg
        return market_cfg

    def _market_display_name_for_id(self, market_id: str) -> str:
        normalized = str(market_id or "polymarket").strip().lower()
        try:
            return self.adapter_registry.get_metadata(normalized).display_name
        except Exception:
            return normalized

    def _selected_market_display_name(self) -> str:
        return self.adapter_registry.get_metadata(self.cfg.selected_market_id).display_name

    def _selected_market_status_text(self, adapter: Optional[MarketAdapter] = None) -> str:
        adapter = adapter or self._get_selected_market_adapter()
        health = adapter.health_check()
        message = str(health.get("message") or "").strip()
        if health.get("verified_blocker"):
            return f"{adapter.display_name}: verified blocked. {message}"
        capabilities = adapter.capabilities
        read_supported = (
            capabilities.market_discovery
            or capabilities.event_listing
            or capabilities.price_reading
            or capabilities.orderbook_reading
        )
        live_status = "no"
        if capabilities.live_trading:
            if not adapter.config_bool("live_trading_enabled", False):
                live_status = "guarded/off"
            elif adapter.config_bool("live_trading_kill_switch", False):
                live_status = "kill-switch"
            elif not (
                adapter.config_bool("live_trading_confirmed", False)
                or adapter.config_bool("live_trading_acknowledged", False)
            ):
                live_status = "needs-confirmation"
            else:
                live_status = "armed"
        return (
            f"{adapter.display_name}: adapter loaded. "
            f"Config {'enabled' if market_config_enabled(self.cfg, adapter.market_id) else 'disabled'}; "
            f"Alerts {'yes' if capabilities.alerts else 'no'}; "
            f"read-only {'yes' if read_supported else 'no'}; "
            f"paper {'yes' if capabilities.paper_trading else 'no'}; "
            f"live {live_status}; "
            f"copy {'yes' if capabilities.copy_trading else 'no'}."
        )

    def _market_disabled_message(self, market_id: str, feature: str) -> str:
        display_name = App._market_display_name_for_id(self, market_id)
        return (
            f"{display_name} is disabled in local market config. "
            f"Enable it in Market Safety before using {feature}."
        )

    def _require_market_enabled(self, market_id: str, feature: str) -> bool:
        normalized = str(market_id or "polymarket").strip().lower()
        if market_config_enabled(self.cfg, normalized):
            return True
        message = App._market_disabled_message(self, normalized, feature)
        if hasattr(self, "status_var"):
            self.status_var.set(message)
        if hasattr(self, "paper_status_var"):
            self.paper_status_var.set(message)
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("log", f"[market] {message}"))
        messagebox.showinfo("Market disabled", message)
        return False

    def _require_polymarket_selected(self, feature: str) -> bool:
        if self.cfg.selected_market_id == "polymarket":
            return True
        adapter = self._get_selected_market_adapter()
        adapter_status = self._selected_market_status_text(adapter)
        message = (
            f"{feature} is currently implemented only for Polymarket. "
            f"{adapter.display_name} is visible as a market adapter entry, "
            "but this GUI workflow has not been generalized for that market yet. "
            f"Selected adapter status: {adapter_status}"
        )
        self.status_var.set(message)
        if hasattr(self, "market_status_var"):
            self.market_status_var.set(adapter_status)
        self.ui_queue.put(("log", f"[market] {message}"))
        messagebox.showinfo("Unsupported market", message)
        return False

    def _require_selected_market_capability(self, feature: str, capability: str) -> bool:
        """Guard a selected-market workflow using the adapter capability matrix."""

        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        if not market_config_enabled(self.cfg, market_id):
            return self._require_market_enabled(market_id, feature)
        try:
            adapter = self._get_selected_market_adapter()
        except Exception as exc:
            message = f"{feature} is unavailable: {exc}"
            self.status_var.set(message)
            self.ui_queue.put(("log", f"[market] {message}"))
            messagebox.showinfo("Market unavailable", message)
            return False
        if not getattr(adapter.capabilities, capability, False):
            message = f"{feature} is not supported for {adapter.display_name}."
            self.status_var.set(message)
            self.ui_queue.put(("log", f"[market] {message}"))
            messagebox.showinfo("Unsupported market", message)
            return False
        return True

    def _on_market_change(self):
        market_id = market_id_from_choice(self.market_var.get())
        if market_id not in self.adapter_registry.list_market_ids():
            market_id = "polymarket"
        if market_id == self.cfg.selected_market_id:
            return
        self.cfg.selected_market_id = market_id
        save_config(self.cfg)
        self.market_var.set(self._market_label_for_id(market_id))
        adapter = self._get_selected_market_adapter()
        health = adapter.health_check()
        adapter_status = self._selected_market_status_text(adapter)
        if hasattr(self, "market_status_var"):
            self.market_status_var.set(adapter_status)
        if hasattr(self, "paper_market_var"):
            self.paper_market_var.set(market_id)
            App._refresh_paper_market_state(self)
        if hasattr(self, "safety_market_var"):
            App._refresh_market_safety_tab(self)
        if health.get("ok"):
            msg = f"Selected market: {adapter.display_name}."
        else:
            msg = f"Selected market: {adapter.display_name}. {health.get('message')}"
        self.status_var.set(msg)
        self.ui_queue.put(("log", f"[market] {msg}"))

    def _build_ui(self):
        topbar = ttk.Frame(self, style="CommandBar.TFrame", padding=(14, 12))
        topbar.pack(fill="x", padx=12, pady=(12, 0))

        header = ttk.Frame(topbar, style="CommandBar.TFrame")
        header.pack(fill="x")
        brand = ttk.Frame(header, style="CommandBar.TFrame")
        brand.pack(side="left", fill="x", expand=True)
        ttk.Label(brand, text="MarketSentinel Command Center", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(brand, text="Alerts, safety, paper trading, wallet tracking, and guarded copy execution", style="AppSubtitle.TLabel").pack(anchor="w")

        mode_bar = ttk.Frame(topbar, style="CommandBar.TFrame")
        mode_bar.pack(fill="x", pady=(10, 0))

        ttk.Label(mode_bar, text="Market:").pack(side="left")
        self.market_var = tk.StringVar(value=self._market_label_for_id(self.cfg.selected_market_id))
        self.market_combo = ttk.Combobox(
            mode_bar,
            textvariable=self.market_var,
            values=self._market_choices(),
            state="readonly",
            width=58,
        )
        self.market_combo.pack(side="left", padx=(6, 18))
        self.market_combo.bind("<<ComboboxSelected>>", lambda e: self._on_market_change())

        ttk.Label(mode_bar, text="Theme:").pack(side="left")
        self.theme_var = tk.StringVar(value=self._theme_label(self.cfg.theme))
        self.theme_combo = ttk.Combobox(
            mode_bar,
            textvariable=self.theme_var,
            values=["Light", "Dark"],
            state="readonly",
            width=8,
        )
        self.theme_combo.pack(side="left", padx=(6, 18))
        self.theme_combo.bind("<<ComboboxSelected>>", lambda e: self._on_theme_change())

        ttk.Label(mode_bar, text="Design:").pack(side="left")
        self.ui_design_var = tk.StringVar(value=self._ui_design_label(self.cfg.ui_design))
        self.ui_design_combo = ttk.Combobox(
            mode_bar,
            textvariable=self.ui_design_var,
            values=list(UI_DESIGN_LABELS.values()),
            state="readonly",
            width=16,
        )
        self.ui_design_combo.pack(side="left", padx=(6, 0))
        self.ui_design_combo.bind("<<ComboboxSelected>>", lambda e: self._on_ui_design_change())

        self.market_status_var = tk.StringVar(value=self._selected_market_status_text())
        ttk.Label(
            self,
            textvariable=self.market_status_var,
            anchor="w",
            style="Status.TLabel",
            wraplength=1100,
        ).pack(fill="x", padx=10, pady=(4, 6))

        nb = ttk.Notebook(self)
        self.notebook = nb
        nb.pack(fill="both", expand=True)

        self.tab_alerts = ttk.Frame(nb)
        self.tab_paper = ttk.Frame(nb)
        self.tab_safety = ttk.Frame(nb)
        self.tab_wallets = ttk.Frame(nb)
        self.tab_analytics = ttk.Frame(nb)
        self.tab_copy = ttk.Frame(nb)
        self.tab_logs = ttk.Frame(nb)
        self.tab_about = ttk.Frame(nb)

        nb.add(self.tab_alerts, text="Markets & Alerts")
        nb.add(self.tab_paper, text="Paper Trading")
        nb.add(self.tab_safety, text="Market Safety")
        nb.add(self.tab_wallets, text="Wallet Tracker")
        nb.add(self.tab_analytics, text="Polymarket Analytics")
        nb.add(self.tab_copy, text="Copy Trading")
        nb.add(self.tab_logs, text="Logs")
        nb.add(self.tab_about, text="About")

        self._build_alerts_tab()
        self._build_paper_tab()
        self._build_market_safety_tab()
        self._build_wallets_tab()
        self._build_analytics_tab()
        self._build_copy_tab()
        self._build_logs_tab()
        self._build_about_tab()

        # status bar
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        status.pack(fill="x", side="bottom")

    # ------------------ Theme ------------------

    def _build_theme_palettes(self) -> Dict[str, Dict[str, str]]:
        classic_light = {
            "bg": "#f6f4ef",
            "surface": "#f6f4ef",
            "surface_alt": "#e9e5dd",
            "fg": "#1f1f1f",
            "heading": "#161616",
            "muted": "#6b6b6b",
            "accent": "#2b6e6d",
            "accent_hover": "#245a59",
            "field_bg": "#ffffff",
            "field_fg": "#1f1f1f",
            "border": "#c9c3b8",
            "tab_bg": "#e9e5dd",
            "tab_active_bg": "#f6f4ef",
            "select_bg": "#dfe8e6",
            "select_fg": "#1f1f1f",
            "log_bg": "#fbfaf7",
            "button_bg": "#e6e1d8",
            "button_fg": "#1f1f1f",
            "font_family": "Segoe UI",
            "font_size": "9",
            "title_size": "13",
            "rowheight": "24",
            "button_padding": "8 4",
            "tab_padding": "10 6",
        }
        classic_dark = {
            "bg": "#1e1f24",
            "surface": "#1e1f24",
            "surface_alt": "#2a2c33",
            "fg": "#e9e6df",
            "heading": "#f5f2eb",
            "muted": "#a6a9b3",
            "accent": "#63b7af",
            "accent_hover": "#4fa79d",
            "field_bg": "#2a2c33",
            "field_fg": "#e9e6df",
            "border": "#3b3f48",
            "tab_bg": "#2a2c33",
            "tab_active_bg": "#1e1f24",
            "select_bg": "#394048",
            "select_fg": "#e9e6df",
            "log_bg": "#15171b",
            "button_bg": "#2f323a",
            "button_fg": "#e9e6df",
            "font_family": "Segoe UI",
            "font_size": "9",
            "title_size": "13",
            "rowheight": "24",
            "button_padding": "8 4",
            "tab_padding": "10 6",
        }
        return {
            "light": classic_light,
            "dark": classic_dark,
            "classic:light": classic_light,
            "classic:dark": classic_dark,
            "aurora_2026:light": {
                **classic_light,
                "bg": "#eef3f7",
                "surface": "#ffffff",
                "surface_alt": "#edf4f7",
                "fg": "#182028",
                "heading": "#0f1720",
                "muted": "#5c6975",
                "accent": "#0f8b8d",
                "accent_hover": "#0b7378",
                "field_bg": "#f8fbff",
                "field_fg": "#182028",
                "border": "#c9d7df",
                "tab_bg": "#e4edf2",
                "tab_active_bg": "#ffffff",
                "select_bg": "#d6f4f0",
                "select_fg": "#0f1720",
                "log_bg": "#f9fbfd",
                "button_bg": "#e2f3f2",
                "button_fg": "#0f1720",
                "font_size": "10",
                "title_size": "16",
                "rowheight": "28",
                "button_padding": "12 7",
                "tab_padding": "14 8",
            },
            "aurora_2026:dark": {
                **classic_dark,
                "bg": "#0f141b",
                "surface": "#151d27",
                "surface_alt": "#1c2733",
                "fg": "#edf6f7",
                "heading": "#ffffff",
                "muted": "#9fb0bd",
                "accent": "#5eead4",
                "accent_hover": "#38c7bd",
                "field_bg": "#0d1621",
                "field_fg": "#edf6f7",
                "border": "#2a3a4a",
                "tab_bg": "#182231",
                "tab_active_bg": "#233041",
                "select_bg": "#123f46",
                "select_fg": "#f8feff",
                "log_bg": "#0a1017",
                "button_bg": "#18343a",
                "button_fg": "#f8feff",
                "font_size": "10",
                "title_size": "16",
                "rowheight": "28",
                "button_padding": "12 7",
                "tab_padding": "14 8",
            },
            "graphite_2026:light": {
                **classic_light,
                "bg": "#f3f5f7",
                "surface": "#ffffff",
                "surface_alt": "#e8edf2",
                "fg": "#181b20",
                "heading": "#0d1014",
                "muted": "#626b76",
                "accent": "#2563eb",
                "accent_hover": "#1d4ed8",
                "field_bg": "#fbfcfe",
                "field_fg": "#181b20",
                "border": "#ccd3db",
                "tab_bg": "#e7ebf0",
                "tab_active_bg": "#ffffff",
                "select_bg": "#dde7ff",
                "select_fg": "#0d1014",
                "log_bg": "#f8fafc",
                "button_bg": "#e7ecf4",
                "button_fg": "#111827",
                "font_size": "10",
                "title_size": "16",
                "rowheight": "28",
                "button_padding": "12 7",
                "tab_padding": "14 8",
            },
            "graphite_2026:dark": {
                **classic_dark,
                "bg": "#101217",
                "surface": "#181c23",
                "surface_alt": "#222832",
                "fg": "#f0f2f5",
                "heading": "#ffffff",
                "muted": "#a4adb8",
                "accent": "#7dd3fc",
                "accent_hover": "#38bdf8",
                "field_bg": "#11151c",
                "field_fg": "#f0f2f5",
                "border": "#2e3541",
                "tab_bg": "#1c222b",
                "tab_active_bg": "#27313d",
                "select_bg": "#1e3a5f",
                "select_fg": "#ffffff",
                "log_bg": "#0b0e13",
                "button_bg": "#25313d",
                "button_fg": "#f8fafc",
                "font_size": "10",
                "title_size": "16",
                "rowheight": "28",
                "button_padding": "12 7",
                "tab_padding": "14 8",
            },
            "sentinel_2027:light": {
                **classic_light,
                "bg": "#eef1f3",
                "surface": "#ffffff",
                "surface_alt": "#edf2f5",
                "fg": "#182027",
                "heading": "#0a1118",
                "muted": "#596672",
                "accent": "#119c87",
                "accent_hover": "#0d806f",
                "field_bg": "#f8fafb",
                "field_fg": "#111820",
                "border": "#d5dde3",
                "tab_bg": "#dfe7ed",
                "tab_active_bg": "#ffffff",
                "select_bg": "#d8f2ec",
                "select_fg": "#071412",
                "log_bg": "#f7f9fb",
                "button_bg": "#e5ecef",
                "button_fg": "#111820",
                "button_active_bg": "#d9e3e8",
                "button_pressed_bg": "#119c87",
                "tab_hover_bg": "#edf2f5",
                "command_borderwidth": "0",
                "hero_borderwidth": "0",
                "card_borderwidth": "0",
                "metric_borderwidth": "0",
                "frame_relief": "flat",
                "font_size": "10",
                "title_size": "17",
                "rowheight": "30",
                "button_padding": "16 8",
                "tab_padding": "18 10",
                "notebook_tabmargins": "14 8 14 0",
            },
            "sentinel_2027:dark": {
                **classic_dark,
                "bg": "#0b1015",
                "surface": "#121922",
                "surface_alt": "#1b2430",
                "fg": "#edf3f5",
                "heading": "#ffffff",
                "muted": "#94a3ad",
                "accent": "#2dd4bf",
                "accent_hover": "#14b8a6",
                "field_bg": "#0f151d",
                "field_fg": "#edf3f5",
                "border": "#25313d",
                "tab_bg": "#121922",
                "tab_active_bg": "#1f2a36",
                "select_bg": "#123d38",
                "select_fg": "#f8fffe",
                "log_bg": "#070b10",
                "button_bg": "#1d2833",
                "button_fg": "#f2f7f8",
                "button_active_bg": "#273542",
                "button_pressed_bg": "#2dd4bf",
                "tab_hover_bg": "#182330",
                "command_borderwidth": "0",
                "hero_borderwidth": "0",
                "card_borderwidth": "0",
                "metric_borderwidth": "0",
                "frame_relief": "flat",
                "font_size": "10",
                "title_size": "17",
                "rowheight": "30",
                "button_padding": "16 8",
                "tab_padding": "18 10",
                "notebook_tabmargins": "14 8 14 0",
            },
        }

    def _palette_for(self, theme: str, ui_design: str) -> Dict[str, str]:
        theme = self._normalize_theme(theme)
        ui_design = self._normalize_ui_design(ui_design)
        return self._themes.get(f"{ui_design}:{theme}", self._themes[f"classic:{theme}"])

    def _normalize_theme(self, theme: str) -> str:
        return "dark" if str(theme).strip().lower() == "dark" else "light"

    def _normalize_ui_design(self, ui_design: str) -> str:
        value = str(ui_design or "aurora_2026").strip().lower().replace("-", "_").replace(" ", "_")
        return value if value in UI_DESIGN_LABELS else "aurora_2026"

    def _theme_label(self, theme: str) -> str:
        return "Dark" if self._normalize_theme(theme) == "dark" else "Light"

    def _theme_from_label(self, label: str) -> str:
        return "dark" if str(label).strip().lower() == "dark" else "light"

    def _ui_design_label(self, ui_design: str) -> str:
        return UI_DESIGN_LABELS.get(self._normalize_ui_design(ui_design), UI_DESIGN_LABELS["aurora_2026"])

    def _ui_design_from_label(self, label: str) -> str:
        normalized = str(label or "").strip().lower()
        if normalized in UI_DESIGN_BY_LABEL:
            return UI_DESIGN_BY_LABEL[normalized]
        return self._normalize_ui_design(normalized)

    def _on_theme_change(self):
        theme = self._theme_from_label(self.theme_var.get())
        if theme == self.cfg.theme:
            return
        self.cfg.theme = theme
        save_config(self.cfg)
        self._apply_theme(theme, self.cfg.ui_design)

    def _on_ui_design_change(self):
        ui_design = self._ui_design_from_label(self.ui_design_var.get())
        if ui_design == self.cfg.ui_design:
            return
        self.cfg.ui_design = ui_design
        save_config(self.cfg)
        self._apply_theme(self.cfg.theme, ui_design)

    def _apply_theme(self, theme: str, ui_design: Optional[str] = None):
        theme = self._normalize_theme(theme)
        ui_design = self._normalize_ui_design(ui_design or getattr(self.cfg, "ui_design", "aurora_2026"))
        palette = self._palette_for(theme, ui_design)
        self._palette = palette

        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        bg = palette["bg"]
        fg = palette["fg"]
        muted = palette["muted"]
        field_bg = palette["field_bg"]
        field_fg = palette["field_fg"]
        border = palette["border"]
        tab_bg = palette["tab_bg"]
        tab_active_bg = palette["tab_active_bg"]
        select_bg = palette["select_bg"]
        select_fg = palette["select_fg"]
        button_bg = palette["button_bg"]
        button_fg = palette["button_fg"]
        accent = palette["accent"]
        accent_hover = palette["accent_hover"]
        log_bg = palette["log_bg"]
        surface = palette["surface"]
        surface_alt = palette["surface_alt"]
        heading = palette["heading"]
        font_family = palette.get("font_family", "Segoe UI")
        font_size = int(palette.get("font_size", "9"))
        title_size = int(palette.get("title_size", "13"))
        rowheight = int(palette.get("rowheight", "24"))
        button_padding = tuple(int(part) for part in palette.get("button_padding", "8 4").split())
        tab_padding = tuple(int(part) for part in palette.get("tab_padding", "10 6").split())
        notebook_tabmargins = tuple(int(part) for part in palette.get("notebook_tabmargins", "0 6 0 0").split())
        frame_relief = palette.get("frame_relief", "solid")
        command_borderwidth = int(palette.get("command_borderwidth", "1"))
        hero_borderwidth = int(palette.get("hero_borderwidth", "1"))
        card_borderwidth = int(palette.get("card_borderwidth", "1"))
        metric_borderwidth = int(palette.get("metric_borderwidth", "1"))
        button_active_bg = palette.get("button_active_bg", surface_alt)
        button_pressed_bg = palette.get("button_pressed_bg", accent)
        tab_hover_bg = palette.get("tab_hover_bg", surface_alt)

        self.configure(background=bg)

        self.style.configure(".", background=bg, foreground=fg, font=(font_family, font_size), borderwidth=0)
        self.style.configure("TFrame", background=bg)
        self.style.configure("Page.TFrame", background=bg)
        self.style.configure("CommandBar.TFrame", background=surface, borderwidth=command_borderwidth, relief=frame_relief)
        self.style.configure("Hero.TFrame", background=surface, borderwidth=hero_borderwidth, relief=frame_relief)
        self.style.configure("Card.TFrame", background=surface, borderwidth=card_borderwidth, relief=frame_relief)
        self.style.configure("PanelBody.TFrame", background=surface)
        self.style.configure("MetricCard.TFrame", background=surface_alt, borderwidth=metric_borderwidth, relief=frame_relief)
        self.style.configure("TLabel", background=bg, foreground=fg)
        self.style.configure("AppTitle.TLabel", background=surface, foreground=heading, font=(font_family, title_size, "bold"))
        self.style.configure("AppSubtitle.TLabel", background=surface, foreground=muted, font=(font_family, font_size))
        self.style.configure("HeroTitle.TLabel", background=surface, foreground=heading, font=(font_family, title_size + 3, "bold"))
        self.style.configure("HeroSubtitle.TLabel", background=surface, foreground=muted, font=(font_family, font_size + 1))
        self.style.configure("SectionTitle.TLabel", background=surface, foreground=heading, font=(font_family, font_size + 2, "bold"))
        self.style.configure("Muted.TLabel", background=surface, foreground=muted, font=(font_family, font_size))
        self.style.configure("MetricLabel.TLabel", background=surface_alt, foreground=muted, font=(font_family, font_size))
        self.style.configure("MetricValue.TLabel", background=surface_alt, foreground=heading, font=(font_family, title_size + 1, "bold"))
        self.style.configure("Status.TLabel", background=bg, foreground=muted, font=(font_family, font_size))
        self.style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=border)
        self.style.configure("TLabelframe.Label", background=bg, foreground=heading, font=(font_family, font_size, "bold"))

        self.style.configure(
            "TButton",
            background=button_bg,
            foreground=button_fg,
            bordercolor=button_bg,
            lightcolor=button_bg,
            darkcolor=button_bg,
            focuscolor=button_bg,
            borderwidth=0,
            relief="flat",
            padding=button_padding,
        )
        self.style.configure(
            "Accent.TButton",
            background=accent,
            foreground=select_fg,
            bordercolor=accent,
            lightcolor=accent,
            darkcolor=accent,
            focuscolor=accent,
            borderwidth=0,
            relief="flat",
            padding=button_padding,
        )
        self.style.map(
            "TButton",
            background=[("disabled", surface_alt), ("pressed", button_pressed_bg), ("active", button_active_bg)],
            foreground=[("disabled", muted), ("active", button_fg), ("pressed", select_fg)],
            bordercolor=[("active", button_active_bg), ("pressed", button_pressed_bg)],
            lightcolor=[("active", button_active_bg), ("pressed", button_pressed_bg)],
            darkcolor=[("active", button_active_bg), ("pressed", button_pressed_bg)],
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", accent_hover), ("pressed", accent_hover), ("disabled", button_bg)],
            foreground=[("active", select_fg), ("pressed", select_fg), ("disabled", muted)],
            bordercolor=[("active", accent_hover), ("pressed", accent_hover), ("disabled", button_bg)],
            lightcolor=[("active", accent_hover), ("pressed", accent_hover), ("disabled", button_bg)],
            darkcolor=[("active", accent_hover), ("pressed", accent_hover), ("disabled", button_bg)],
        )

        self.style.configure("TEntry", fieldbackground=field_bg, foreground=field_fg, background=bg, bordercolor=border, lightcolor=border, darkcolor=border)
        self.style.map("TEntry", fieldbackground=[("disabled", tab_bg)], foreground=[("disabled", muted)])

        self.style.configure(
            "TCombobox",
            fieldbackground=field_bg,
            background=field_bg,
            foreground=field_fg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            arrowcolor=muted,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", field_bg)],
            foreground=[("readonly", field_fg)],
            selectbackground=[("readonly", select_bg)],
            selectforeground=[("readonly", select_fg)],
        )

        self.style.configure("TCheckbutton", background=bg, foreground=fg)
        self.style.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", muted)])
        self.style.configure("Card.TCheckbutton", background=surface, foreground=fg)
        self.style.map("Card.TCheckbutton", background=[("active", surface)], foreground=[("disabled", muted)])

        self.style.configure("TNotebook", background=bg, bordercolor=bg, borderwidth=0, tabmargins=notebook_tabmargins)
        self.style.configure(
            "TNotebook.Tab",
            background=tab_bg,
            foreground=muted,
            bordercolor=tab_bg,
            lightcolor=tab_bg,
            darkcolor=tab_bg,
            focuscolor=tab_bg,
            borderwidth=0,
            relief="flat",
            padding=tab_padding,
        )
        self.style.map(
            "TNotebook.Tab",
            background=[("active", tab_hover_bg), ("selected", tab_bg)],
            foreground=[("active", heading), ("selected", heading)],
            bordercolor=[("active", tab_hover_bg), ("selected", tab_bg)],
            lightcolor=[("active", tab_hover_bg), ("selected", tab_bg)],
            darkcolor=[("active", tab_hover_bg), ("selected", tab_bg)],
        )

        self.style.configure(
            "Treeview",
            background=field_bg,
            fieldbackground=field_bg,
            foreground=field_fg,
            bordercolor=border,
            rowheight=rowheight,
            font=(font_family, font_size),
        )
        self.style.map("Treeview", background=[("selected", select_bg)], foreground=[("selected", select_fg)])
        self.style.configure(
            "Treeview.Heading",
            background=surface_alt,
            foreground=heading,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            font=(font_family, font_size, "bold"),
            relief="flat",
        )
        self.style.map("Treeview.Heading", background=[("active", tab_active_bg)])

        self.option_add("*TCombobox*Listbox.background", field_bg)
        self.option_add("*TCombobox*Listbox.foreground", field_fg)
        self.option_add("*TCombobox*Listbox.selectBackground", select_bg)
        self.option_add("*TCombobox*Listbox.selectForeground", select_fg)
        self.option_add("*Listbox.background", field_bg)
        self.option_add("*Listbox.foreground", field_fg)
        self.option_add("*Listbox.selectBackground", select_bg)
        self.option_add("*Listbox.selectForeground", select_fg)

        if hasattr(self, "log_text"):
            self.log_text.configure(
                bg=log_bg,
                fg=field_fg,
                insertbackground=field_fg,
                selectbackground=select_bg,
                selectforeground=select_fg,
                highlightbackground=border,
                highlightcolor=border,
            )

        for lb_name in ("outcome_list", "activity_list"):
            lb = getattr(self, lb_name, None)
            if lb is not None:
                lb.configure(
                    bg=field_bg,
                    fg=field_fg,
                    selectbackground=select_bg,
                    selectforeground=select_fg,
                    highlightbackground=border,
                    highlightcolor=border,
                )

        self._apply_native_titlebar(self)

    def _apply_native_titlebar(self, window: tk.Misc) -> None:
        palette = getattr(self, "_palette", {}) or {}
        cfg = getattr(self, "cfg", None)
        theme = self._normalize_theme(getattr(cfg, "theme", "light"))
        dark = theme == "dark"
        caption_color = palette.get("surface", "#151d27" if dark else "#f6f4ef")
        text_color = palette.get("heading", "#ffffff" if dark else "#161616")
        border_color = palette.get("border", "#2a3a4a" if dark else "#c9c3b8")

        def apply_titlebar() -> None:
            _set_windows_titlebar_theme(
                window,
                dark=dark,
                caption_color=caption_color,
                text_color=text_color,
                border_color=border_color,
            )

        apply_titlebar()
        try:
            for delay in (50, 150, 500, 1500):
                window.after(delay, apply_titlebar)
        except Exception:
            pass

    def _icon_path(self) -> Optional[Path]:
        for root in self._resource_roots():
            for path in (root / "assets" / "marketsentinel.ico", root / "assets" / "polymarket.ico"):
                if path.exists():
                    return path
        return None

    def _icon_png_path(self) -> Optional[Path]:
        paths = self._icon_png_paths()
        return paths[0] if paths else None

    def _icon_png_paths(self) -> List[Path]:
        exact_names = tuple(f"marketsentinel-{size}.png" for size in (256, 128, 64, 48, 40, 32, 24, 20, 16))
        exact_paths: List[Path] = []
        fallback_paths: List[Path] = []
        for root in self._resource_roots():
            for name in exact_names:
                path = root / "assets" / "icons" / name
                if path.exists() and path not in exact_paths:
                    exact_paths.append(path)
            for path in (
                root / "assets" / "marketsentinel.png",
                root / "marketsentinel.png",
                root / "frontend" / "public" / "marketsentinel.png",
                root / "assets" / "polymarket.png",
                root / "polymarket.png",
                root / "frontend" / "public" / "polymarket.png",
            ):
                if path.exists() and path not in fallback_paths:
                    fallback_paths.append(path)
        return exact_paths or fallback_paths

    def _resource_roots(self) -> List[Path]:
        return application_resource_roots()

    def _load_icon_image(self):
        self._icon_images = []
        icon_paths = self._icon_png_paths()
        if not icon_paths:
            return
        try:
            images: List[tk.PhotoImage] = []
            for icon_path in icon_paths:
                images.append(tk.PhotoImage(file=str(icon_path)))
            if len(images) == 1:
                base = images[0]
                base_w = base.width()
                for size in (256, 128, 64, 48, 40, 32, 24, 20, 16):
                    if base_w == size:
                        continue
                    if base_w > size and base_w % size == 0:
                        factor = base_w // size
                        images.append(base.subsample(factor, factor))
            self._icon_images = sorted(images, key=lambda image: image.width(), reverse=True)
        except Exception:
            self._icon_images = []

    def _apply_window_icon(self, window: tk.Misc):
        icon_path = self._icon_path()
        if not icon_path:
            icon_path = None
        if self._icon_images:
            try:
                window.iconphoto(True, *self._icon_images)
            except Exception:
                pass
        if icon_path:
            try:
                window.iconbitmap(str(icon_path))
            except Exception:
                pass
            try:
                window.iconbitmap(default=str(icon_path))
            except Exception:
                pass
        self._apply_native_titlebar(window)

    # ------------------ Dependency versions ------------------

    def _requirements_path(self) -> Path:
        for root in self._resource_roots():
            path = root / "requirements.txt"
            if path.exists():
                return path
        return Path(__file__).resolve().parent / "requirements.txt"

    def _pyproject_path(self) -> Optional[Path]:
        for root in self._resource_roots():
            path = root / "pyproject.toml"
            if path.exists():
                return path
        return None

    @staticmethod
    def _parse_requirement_entry(raw: str) -> Optional[Dict[str, str]]:
        line = raw.strip()
        if not line or line.startswith("#"):
            return None
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            return None
        try:
            from packaging.requirements import Requirement

            requirement = Requirement(line)
            if requirement.marker is not None and not requirement.marker.evaluate():
                return None
            extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
            return {
                "name": requirement.name,
                "display": f"{requirement.name}{extras}",
                "spec": str(requirement.specifier),
            }
        except Exception:
            if ";" in line:
                line = line.split(";", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9_.-]+)(\[[^\]]+\])?(.*)$", line)
        if not match:
            return None
        name = match.group(1)
        extras = match.group(2) or ""
        spec = (match.group(3) or "").strip()
        return {"name": name, "display": f"{name}{extras}", "spec": spec}

    def _load_requirements(self) -> List[Dict[str, str]]:
        path = self._requirements_path()
        reqs: List[Dict[str, str]] = []
        if not path.exists():
            pyproject = self._pyproject_path()
            if not pyproject:
                return []
            try:
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                dependencies = data.get("project", {}).get("dependencies", [])
            except Exception:
                return []
            for raw in dependencies:
                parsed = self._parse_requirement_entry(str(raw))
                if parsed:
                    reqs.append(parsed)
            return reqs
        for raw in path.read_text(encoding="utf-8").splitlines():
            parsed = self._parse_requirement_entry(raw)
            if parsed:
                reqs.append(parsed)
        return reqs

    def _project_version(self) -> str:
        try:
            return importlib_metadata.version("market-sentinel")
        except importlib_metadata.PackageNotFoundError:
            pass
        pyproject = self._pyproject_path()
        if not pyproject:
            return "unknown"
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # type: ignore
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return str(data.get("project", {}).get("version") or "unknown")
        except Exception:
            return "unknown"

    def _get_installed_version(self, package: str) -> str:
        try:
            return importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:
            pass
        module_names = DEPENDENCY_IMPORT_FALLBACKS.get(package, (package.replace("-", "_"),))
        for module_name in module_names:
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            version = str(getattr(module, "__version__", "") or getattr(module, "version", "") or "").strip()
            return version or "installed"
        return ""

    def _fetch_latest_version(self, package: str) -> str:
        url = f"https://pypi.org/pypi/{package}/json"
        req = urllib_request.Request(
            url,
            headers={"User-Agent": APP_USER_AGENT},
        )
        try:
            with urllib_request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib_error.URLError, urllib_error.HTTPError) as exc:
            raise RuntimeError(str(exc)) from exc
        version = str(data.get("info", {}).get("version", "")).strip()
        return version

    def _is_up_to_date(self, installed: str, latest: str) -> bool:
        try:
            from packaging.version import Version
        except Exception:
            return installed == latest
        try:
            return Version(installed) >= Version(latest)
        except Exception:
            return installed == latest

    def _refresh_dependency_table(self, rows: Optional[List[Dict[str, str]]] = None):
        for iid in self.deps_tree.get_children():
            self.deps_tree.delete(iid)

        if rows is None:
            rows = []
            for req in self._requirements:
                installed = self._get_installed_version(req["name"])
                rows.append(
                    {
                        "display": req["display"],
                        "spec": req["spec"],
                        "installed": installed or "not installed",
                        "latest": "",
                        "status": "",
                    }
                )

        if not rows:
            self.deps_tree.insert("", "end", values=("No dependency metadata found", "", "", "", ""))
            if hasattr(self, "check_versions_btn"):
                self.check_versions_btn.configure(state="disabled")
            self.dep_status_var.set("No dependency metadata found.")
            return

        if hasattr(self, "check_versions_btn"):
            self.check_versions_btn.configure(state="normal")
        if hasattr(self, "dep_status_var") and not self.dep_status_var.get().startswith("Checked"):
            self.dep_status_var.set(f"Loaded {len(rows)} dependencies.")

        for row in rows:
            spec = row.get("spec") or "-"
            self.deps_tree.insert(
                "",
                "end",
                values=(
                    row.get("display", ""),
                    spec,
                    row.get("installed", ""),
                    row.get("latest", ""),
                    row.get("status", ""),
                ),
            )

    def check_dependency_versions(self):
        if self._dep_check_running:
            return
        if not self._requirements:
            self.dep_status_var.set("No dependency metadata found.")
            return
        self._dep_check_running = True
        self.dep_status_var.set("Checking versions...")
        self.check_versions_btn.configure(state="disabled")
        threading.Thread(target=self._check_dependency_versions_bg, daemon=True).start()

    def _check_dependency_versions_bg(self):
        rows: List[Dict[str, str]] = []
        errors: List[str] = []
        for req in self._requirements:
            name = req["name"]
            installed = self._get_installed_version(name) or "not installed"
            latest = ""
            try:
                latest = self._fetch_latest_version(name)
            except Exception as exc:
                errors.append(f"{req['display']}: {exc}")
            if installed == "not installed":
                status = "missing"
            elif installed == "installed":
                status = "installed"
            elif latest:
                status = "ok" if self._is_up_to_date(installed, latest) else "outdated"
            else:
                status = "unknown"
            rows.append(
                {
                    "display": req["display"],
                    "spec": req["spec"],
                    "installed": installed,
                    "latest": latest or "-",
                    "status": status,
                }
            )
        self.ui_queue.put(("dep_versions", rows, errors))

    def _build_logs_tab(self):
        frm = self.tab_logs
        self.log_text = tk.Text(frm, height=20, wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.log("App started. Price WS connected in background.")

    def _build_about_tab(self):
        frm = self.tab_about
        frm.configure(style="Page.TFrame")
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(1, weight=1)

        summary = ttk.Frame(frm, style="Hero.TFrame", padding=(18, 16))
        summary.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, text=APP_TITLE, style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            summary,
            text="Local command center for alerts, analytics, paper trading, wallet tracking, and guarded copy execution.",
            style="HeroSubtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        meta = ttk.Frame(summary, style="Hero.TFrame")
        meta.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 0))
        for idx, (label, value) in enumerate(
            (
                ("Version", self._project_version()),
                ("Python", sys.version.split()[0]),
                ("Theme", self._theme_label(self.cfg.theme)),
            )
        ):
            tile = ttk.Frame(meta, style="MetricCard.TFrame", padding=(14, 10))
            tile.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 8, 0))
            ttk.Label(tile, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            ttk.Label(tile, text=value, style="MetricValue.TLabel").pack(anchor="w", pady=(2, 0))

        deps = ttk.Frame(frm, style="Card.TFrame", padding=(14, 12))
        deps.grid(row=1, column=0, sticky="nsew", padx=14, pady=(8, 14))
        deps.columnconfigure(0, weight=1)
        deps.rowconfigure(1, weight=1)

        deps_header = ttk.Frame(deps, style="PanelBody.TFrame")
        deps_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        deps_header.columnconfigure(0, weight=1)
        ttk.Label(deps_header, text="Dependency versions", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        self.dep_status_var = tk.StringVar(value="Versions not checked.")
        ttk.Label(deps_header, textvariable=self.dep_status_var, style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.check_versions_btn = ttk.Button(
            deps_header,
            text="Check versions",
            style="Accent.TButton",
            command=self.check_dependency_versions,
        )
        self.check_versions_btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        cols = ("package", "required", "installed", "latest", "status")
        self.deps_tree = ttk.Treeview(deps, columns=cols, show="headings", height=12)
        for c in cols:
            self.deps_tree.heading(c, text=c)
        self.deps_tree.column("package", width=260, stretch=True)
        self.deps_tree.column("required", width=190, stretch=False, anchor="center")
        self.deps_tree.column("installed", width=150, stretch=False, anchor="center")
        self.deps_tree.column("latest", width=140, stretch=False, anchor="center")
        self.deps_tree.column("status", width=110, stretch=False, anchor="center")
        dep_scroll = ttk.Scrollbar(deps, orient="vertical", command=self.deps_tree.yview)
        self.deps_tree.configure(yscrollcommand=dep_scroll.set)
        self.deps_tree.grid(row=1, column=0, sticky="nsew")
        dep_scroll.grid(row=1, column=1, sticky="ns")

        self._refresh_dependency_table()

    def _build_alerts_tab(self):
        frm = self.tab_alerts

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Market search, id, slug, or URL:").grid(row=0, column=0, sticky="w")
        self.market_entry = ttk.Entry(top, width=60)
        self.market_entry.grid(row=0, column=1, padx=5)

        ttk.Button(top, text="Fetch Market", command=self.fetch_market).grid(row=0, column=2, padx=5)

        self.market_info_var = tk.StringVar(value="No market loaded.")
        ttk.Label(top, textvariable=self.market_info_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6,0))

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, padx=10, pady=10)

        # Outcomes list
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Market outcomes (select one):").pack(anchor="w")
        self.outcome_list = tk.Listbox(left, height=12)
        self.outcome_list.pack(fill="both", expand=True)
        self.outcome_list.bind("<<ListboxSelect>>", lambda e: self._on_outcome_selected())

        # Alert form
        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=(10,0))

        ttk.Label(right, text="Create price alert:").grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(right, text="Label:").grid(row=1, column=0, sticky="e")
        self.alert_label_entry = ttk.Entry(right, width=35)
        self.alert_label_entry.grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(right, text="Threshold (0..1):").grid(row=2, column=0, sticky="e")
        self.alert_threshold_entry = ttk.Entry(right, width=10)
        self.alert_threshold_entry.grid(row=2, column=1, sticky="w", pady=2)

        ttk.Label(right, text="Direction:").grid(row=3, column=0, sticky="e")
        self.alert_dir_var = tk.StringVar(value="above")
        ttk.Combobox(right, textvariable=self.alert_dir_var, values=["above","below"], width=10, state="readonly").grid(row=3, column=1, sticky="w", pady=2)

        ttk.Label(right, text="Source:").grid(row=4, column=0, sticky="e")
        self.alert_src_var = tk.StringVar(value="last_trade")
        ttk.Combobox(right, textvariable=self.alert_src_var, values=["last_trade","midpoint","best_bid","best_ask"], width=12, state="readonly").grid(row=4, column=1, sticky="w", pady=2)

        self.alert_once_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="Trigger once (disable after firing)", variable=self.alert_once_var).grid(row=5, column=1, sticky="w", pady=2)

        ttk.Button(right, text="Add Alert", command=self.add_alert).grid(row=6, column=1, sticky="w", pady=(8,2))

        # Alerts table
        bottom = ttk.Frame(frm)
        bottom.pack(fill="both", expand=True, padx=10, pady=(0,10))

        cols = ("market","label","token","dir","threshold","source","enabled","triggered","last")
        self.alert_tree = ttk.Treeview(bottom, columns=cols, show="headings", height=10)
        for c in cols:
            self.alert_tree.heading(c, text=c)
        self.alert_tree.column("market", width=120)
        self.alert_tree.column("label", width=180)
        self.alert_tree.column("token", width=220)
        self.alert_tree.column("dir", width=60)
        self.alert_tree.column("threshold", width=80)
        self.alert_tree.column("source", width=90)
        self.alert_tree.column("enabled", width=70)
        self.alert_tree.column("triggered", width=70)
        self.alert_tree.column("last", width=80)
        self.alert_tree.pack(fill="both", expand=True, side="left")

        btns = ttk.Frame(bottom)
        btns.pack(fill="y", side="left", padx=(8,0))

        ttk.Button(btns, text="Toggle enable", command=self.toggle_selected_alert).pack(fill="x", pady=2)
        ttk.Button(btns, text="Delete", command=self.delete_selected_alert).pack(fill="x", pady=2)
        ttk.Button(btns, text="Resubscribe WS", command=self.resubscribe_ws).pack(fill="x", pady=10)

        self._refresh_alert_table()

        self._market_loaded: Optional[Dict[str, Any]] = None
        self._market_outcomes: List[Any] = []
        self._selected_token_id: Optional[str] = None
        self._selected_alert_market_id: str = self.cfg.selected_market_id

    def _build_paper_tab(self):
        frm = self.tab_paper

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=10, pady=10)

        self.paper_selected_var = tk.StringVar(value="No contract selected.")
        ttk.Label(top, textvariable=self.paper_selected_var, wraplength=900).pack(anchor="w")

        form = ttk.Frame(frm)
        form.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Label(form, text="Market:").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        self.paper_market_var = tk.StringVar(value=self.cfg.selected_market_id)
        self.paper_market_combo = ttk.Combobox(
            form,
            textvariable=self.paper_market_var,
            values=self.adapter_registry.list_market_ids(),
            width=28,
            state="readonly",
        )
        self.paper_market_combo.grid(row=0, column=1, sticky="w", padx=5, pady=4)
        self.paper_market_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_paper_market_state())

        ttk.Label(form, text="Contract:").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        self.paper_contract_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.paper_contract_var, width=52).grid(row=1, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(form, text="Side:").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        self.paper_side_var = tk.StringVar(value="BUY")
        ttk.Combobox(
            form,
            textvariable=self.paper_side_var,
            values=["BUY", "SELL", "BACK", "LAY"],
            width=10,
            state="readonly",
        ).grid(row=2, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(form, text="Size:").grid(row=3, column=0, sticky="e", padx=5, pady=4)
        self.paper_size_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.paper_size_var, width=12).grid(row=3, column=1, sticky="w", padx=5, pady=4)

        ttk.Label(form, text="Limit:").grid(row=4, column=0, sticky="e", padx=5, pady=4)
        self.paper_limit_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.paper_limit_var, width=12).grid(row=4, column=1, sticky="w", padx=5, pady=4)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Button(btns, text="Use selected contract", command=self.use_selected_contract_for_paper).pack(side="left")
        self.paper_quote_btn = ttk.Button(btns, text="Refresh Quote", command=self.refresh_paper_quote)
        self.paper_quote_btn.pack(side="left", padx=(8, 0))
        self.paper_quote_limit_btn = ttk.Button(btns, text="Use Quote Limit", command=self.use_quote_limit_for_paper)
        self.paper_quote_limit_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Preview Impact", command=self.preview_paper_order_impact).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Preview Live Preflight", command=self.preview_live_preflight).pack(side="left", padx=(8, 0))
        self.paper_submit_btn = ttk.Button(btns, text="Submit Paper Order", command=self.submit_paper_order)
        self.paper_submit_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Use History Order", command=self.use_selected_paper_trade).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Clear History", command=self.clear_paper_history).pack(side="left", padx=(8, 0))

        self.paper_status_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.paper_status_var, wraplength=900).pack(anchor="w", padx=10, pady=(0, 8))

        pos_header = ttk.Frame(frm)
        pos_header.pack(fill="x", padx=10)
        ttk.Label(pos_header, text="Paper exposure summary:").pack(side="left")
        ttk.Button(pos_header, text="Refresh Marks", command=self.refresh_paper_position_marks).pack(side="left", padx=(8, 0))
        ttk.Button(pos_header, text="Refresh Selected Mark", command=self.refresh_selected_paper_position_mark).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(pos_header, text="Clear Selected Mark", command=self.clear_selected_paper_position_mark).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(pos_header, text="Clear Marks", command=self.clear_paper_position_marks).pack(side="left", padx=(8, 0))
        ttk.Button(pos_header, text="Use Position", command=self.use_selected_paper_position).pack(side="left", padx=(8, 0))
        self.paper_position_summary_var = tk.StringVar(value="No paper exposure.")
        ttk.Label(frm, textvariable=self.paper_position_summary_var, wraplength=900).pack(
            anchor="w", padx=10, pady=(2, 4)
        )
        pos_cols = (
            "market",
            "contract",
            "net_size",
            "avg_price",
            "notional",
            "mark",
            "mark_src",
            "mark_time",
            "unrealized",
            "trades",
        )
        self.paper_position_tree = ttk.Treeview(frm, columns=pos_cols, show="headings", height=5)
        for c in pos_cols:
            self.paper_position_tree.heading(c, text=c)
        self.paper_position_tree.column("market", width=120, stretch=False)
        self.paper_position_tree.column("contract", width=190)
        self.paper_position_tree.column("net_size", width=80, stretch=False, anchor="e")
        self.paper_position_tree.column("avg_price", width=80, stretch=False, anchor="e")
        self.paper_position_tree.column("notional", width=90, stretch=False, anchor="e")
        self.paper_position_tree.column("mark", width=80, stretch=False, anchor="e")
        self.paper_position_tree.column("mark_src", width=70, stretch=False, anchor="center")
        self.paper_position_tree.column("mark_time", width=80, stretch=False, anchor="center")
        self.paper_position_tree.column("unrealized", width=95, stretch=False, anchor="e")
        self.paper_position_tree.column("trades", width=60, stretch=False, anchor="center")
        self.paper_position_tree.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(frm, text="Paper order history:").pack(anchor="w", padx=10)
        cols = ("time", "market", "contract", "side", "size", "limit", "accepted", "message")
        self.paper_tree = ttk.Treeview(frm, columns=cols, show="headings", height=12)
        for c in cols:
            self.paper_tree.heading(c, text=c)
        self.paper_tree.column("time", width=90, stretch=False)
        self.paper_tree.column("market", width=120, stretch=False)
        self.paper_tree.column("contract", width=220)
        self.paper_tree.column("side", width=60, stretch=False, anchor="center")
        self.paper_tree.column("size", width=80, stretch=False, anchor="e")
        self.paper_tree.column("limit", width=80, stretch=False, anchor="e")
        self.paper_tree.column("accepted", width=80, stretch=False, anchor="center")
        self.paper_tree.column("message", width=330)
        self.paper_tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._refresh_paper_market_state()
        self._refresh_paper_trade_table()

    def _build_market_safety_tab(self):
        frm = self.tab_safety

        content = ttk.Frame(frm)
        content.pack(fill="both", expand=True, padx=10, pady=10)

        self.safety_market_var = tk.StringVar(value="")
        ttk.Label(content, textvariable=self.safety_market_var).pack(anchor="w", pady=(0, 8))

        form = ttk.Labelframe(content, text="Selected market live safety")
        form.pack(fill="x", pady=(0, 10))

        self.safety_market_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Enable market in local config", variable=self.safety_market_enabled_var).grid(
            row=0,
            column=0,
            sticky="w",
            padx=5,
            pady=5,
        )

        self.safety_live_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Enable live trading", variable=self.safety_live_enabled_var).grid(
            row=1,
            column=0,
            sticky="w",
            padx=5,
            pady=5,
        )

        self.safety_live_confirmed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Acknowledge live-order risk", variable=self.safety_live_confirmed_var).grid(
            row=1,
            column=1,
            sticky="w",
            padx=5,
            pady=5,
        )

        self.safety_kill_switch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Kill switch", variable=self.safety_kill_switch_var).grid(
            row=1,
            column=2,
            sticky="w",
            padx=5,
            pady=5,
        )

        ttk.Label(form, text="Max order size:").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.safety_max_size_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.safety_max_size_var, width=12).grid(row=2, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(form, text="Max notional:").grid(row=3, column=0, sticky="e", padx=5, pady=5)
        self.safety_max_notional_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.safety_max_notional_var, width=12).grid(
            row=3,
            column=1,
            sticky="w",
            padx=5,
            pady=5,
        )

        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=1, columnspan=2, sticky="w", padx=5, pady=10)
        ttk.Button(buttons, text="Save safety settings", command=self.save_market_safety_settings).pack(side="left")
        ttk.Button(buttons, text="Refresh health", command=self._refresh_market_safety_tab).pack(side="left", padx=(8, 0))

        self.safety_status_var = tk.StringVar(value="")
        ttk.Label(content, textvariable=self.safety_status_var, wraplength=900).pack(anchor="w", pady=(0, 8))

        cols = ("field", "value")
        self.safety_tree = ttk.Treeview(content, columns=cols, show="headings", height=13)
        for c in cols:
            self.safety_tree.heading(c, text=c)
        self.safety_tree.column("field", width=220, stretch=False)
        self.safety_tree.column("value", width=760, stretch=True)
        self.safety_tree.pack(fill="both", expand=True)

        self._refresh_market_safety_tab()

    def _build_wallets_tab(self):
        frm = self.tab_wallets

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=10, pady=10)

        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        identity_hint = activity_identity_hint(market_id)
        ttk.Label(top, text=f"Activity identity ({identity_hint}):").grid(row=0, column=0, sticky="w")
        self.wallet_search_entry = ttk.Entry(top, width=50)
        self.wallet_search_entry.grid(row=0, column=1, padx=5)
        ttk.Button(top, text="Search/Add", command=self.search_or_add_wallet).grid(row=0, column=2, padx=5)

        ttk.Label(top, text="Poll interval (sec):").grid(row=1, column=0, sticky="w", pady=(8,0))
        self.poll_interval_var = tk.StringVar(value="10")
        ttk.Entry(top, textvariable=self.poll_interval_var, width=6).grid(row=1, column=1, sticky="w", pady=(8,0))

        self.polling_var = tk.BooleanVar(value=False)
        ttk.Button(top, text="Start / Stop Tracking", command=self.toggle_wallet_polling).grid(row=1, column=2, padx=5, pady=(8,0))

        mid = ttk.Frame(frm)
        mid.pack(fill="both", expand=True, padx=10, pady=10)

        # tracked wallets table
        cols = ("name","wallet","enabled","last_seen")
        self.wallet_tree = ttk.Treeview(mid, columns=cols, show="headings", height=10)
        for c in cols:
            self.wallet_tree.heading(c, text=c)
        self.wallet_tree.column("name", width=160)
        self.wallet_tree.column("wallet", width=340)
        self.wallet_tree.column("enabled", width=80)
        self.wallet_tree.column("last_seen", width=120)
        self.wallet_tree.pack(fill="both", expand=True, side="left")

        btns = ttk.Frame(mid)
        btns.pack(fill="y", side="left", padx=(8,0))
        ttk.Button(btns, text="Toggle enable", command=self.toggle_selected_wallet).pack(fill="x", pady=2)
        ttk.Button(btns, text="Delete", command=self.delete_selected_wallet).pack(fill="x", pady=2)

        # activity feed
        bottom = ttk.Frame(frm)
        bottom.pack(fill="both", expand=True, padx=10, pady=(0,10))

        ttk.Label(bottom, text="Recent activity (newest first):").pack(anchor="w")
        self.activity_list = tk.Listbox(bottom, height=10)
        self.activity_list.pack(fill="both", expand=True)

        self._refresh_wallet_table()

    def _build_analytics_tab(self):
        frm = self.tab_analytics
        frm.configure(style="Page.TFrame")
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)

        hero = ttk.Frame(frm, style="Hero.TFrame", padding=(18, 16))
        hero.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text="Polymarket trader analytics", style="HeroTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            hero,
            text="Rank public leaderboard users by computed ROI %, PnL, volume, or drawdown without opening the web UI.",
            style="HeroSubtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        metric_bar = ttk.Frame(hero, style="Hero.TFrame")
        metric_bar.grid(row=0, column=1, rowspan=2, sticky="e", padx=(18, 0))
        self.lb_returned_metric_var = tk.StringVar(value="0")
        self.lb_scanned_metric_var = tk.StringVar(value="0")
        self.lb_best_roi_metric_var = tk.StringVar(value="-")
        self.lb_mdd_metric_var = tk.StringVar(value="0")
        metrics = (
            ("Returned", self.lb_returned_metric_var),
            ("Scanned", self.lb_scanned_metric_var),
            ("Best ROI", self.lb_best_roi_metric_var),
            ("MDD runs", self.lb_mdd_metric_var),
        )
        for idx, (label, var) in enumerate(metrics):
            card = ttk.Frame(metric_bar, style="MetricCard.TFrame", padding=(14, 10))
            card.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 8, 0))
            ttk.Label(card, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=var, style="MetricValue.TLabel").pack(anchor="w", pady=(2, 0))

        controls = ttk.Frame(frm, style="Card.TFrame", padding=(14, 12))
        controls.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
        for col in range(10):
            controls.columnconfigure(col, weight=1)

        self.lb_sort_var = tk.StringVar(value="ROI %")
        self.lb_direction_var = tk.StringVar(value="High to low")
        self.lb_limit_var = tk.StringVar(value="1000")
        self.lb_scan_limit_var = tk.StringVar(value="1000")
        self.lb_period_var = tk.StringVar(value="All")
        self.lb_category_var = tk.StringVar(value="OVERALL")
        self.lb_compute_mdd_var = tk.BooleanVar(value=False)
        self.lb_fast_scan_var = tk.BooleanVar(value=True)
        self.lb_mdd_mode_var = tk.StringVar(value="Fast public curve")
        self.lb_mdd_scan_limit_var = tk.StringVar(value="100")
        self.lb_min_roi_var = tk.StringVar(value="")
        self.lb_max_roi_var = tk.StringVar(value="")
        self.lb_min_mdd_pct_var = tk.StringVar(value="")
        self.lb_max_mdd_pct_var = tk.StringVar(value="")

        fields = (
            ("Sort", self.lb_sort_var, ["ROI %", "PnL USD", "Volume USD", "MDD %", "MDD USD"], 0, 0, 14),
            ("Direction", self.lb_direction_var, ["High to low", "Low to high"], 0, 1, 12),
            ("Period", self.lb_period_var, ["All", "Day", "Week", "Month"], 0, 2, 10),
            ("Category", self.lb_category_var, ["OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "WEATHER", "ECONOMICS", "TECH", "FINANCE"], 0, 3, 14),
            ("MDD mode", self.lb_mdd_mode_var, ["Fast public curve", "CLOB mark replay"], 1, 3, 16),
        )
        for label, var, values, row, col, width in fields:
            cell = ttk.Frame(controls, style="PanelBody.TFrame")
            cell.grid(row=row, column=col, sticky="ew", padx=5, pady=4)
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Combobox(cell, textvariable=var, values=values, state="readonly", width=width).pack(fill="x")

        numeric_fields = (
            ("Returned", self.lb_limit_var, "1", "1000000", 0, 4),
            ("Scanned", self.lb_scan_limit_var, "1", "1000000", 0, 5),
            ("MDD scan", self.lb_mdd_scan_limit_var, "1", "1000000", 1, 4),
            ("Min ROI %", self.lb_min_roi_var, None, None, 1, 0),
            ("Max ROI %", self.lb_max_roi_var, None, None, 1, 1),
            ("Min MDD %", self.lb_min_mdd_pct_var, None, None, 1, 2),
            ("Max MDD %", self.lb_max_mdd_pct_var, None, None, 1, 5),
        )
        for label, var, min_value, max_value, row, col in numeric_fields:
            cell = ttk.Frame(controls, style="PanelBody.TFrame")
            cell.grid(row=row, column=col, sticky="ew", padx=5, pady=4)
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            entry_kwargs = {"textvariable": var, "width": 12}
            if min_value is not None and max_value is not None:
                entry_kwargs.update({"inputMode": "numeric"})
            entry = ttk.Entry(cell, **{k: v for k, v in entry_kwargs.items() if k != "inputMode"})
            entry.pack(fill="x")

        checks = ttk.Frame(controls, style="PanelBody.TFrame")
        checks.grid(row=0, column=6, rowspan=2, sticky="nsew", padx=(10, 5), pady=4)
        ttk.Checkbutton(
            checks,
            text="Compute MDD",
            variable=self.lb_compute_mdd_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(18, 4))
        ttk.Checkbutton(
            checks,
            text="Fast scan",
            variable=self.lb_fast_scan_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(0, 4))

        actions = ttk.Frame(controls, style="PanelBody.TFrame")
        actions.grid(row=0, column=7, columnspan=3, rowspan=2, sticky="nsew", padx=(10, 0), pady=4)
        ttk.Label(actions, text="Search", style="Muted.TLabel").pack(anchor="w")
        self.lb_fast_roi_btn = ttk.Button(
            actions,
            text="Best ROI <=20% MDD",
            style="Accent.TButton",
            command=self.load_fast_polymarket_roi_mdd,
        )
        self.lb_fast_roi_btn.pack(fill="x", pady=(3, 6))
        self.lb_load_btn = ttk.Button(
            actions,
            text="Load Top ROI",
            style="Accent.TButton",
            command=self.load_polymarket_leaderboard,
        )
        self.lb_load_btn.pack(fill="x", pady=(0, 6))
        self.lb_cancel_btn = ttk.Button(
            actions,
            text="Cancel Scan",
            command=self.cancel_polymarket_leaderboard_scan,
            state="disabled",
        )
        self.lb_cancel_btn.pack(fill="x", pady=(0, 6))
        ttk.Button(actions, text="Export CSV", command=self.export_polymarket_leaderboard).pack(fill="x")

        table_card = ttk.Frame(frm, style="Card.TFrame", padding=(12, 12))
        table_card.grid(row=2, column=0, sticky="nsew", padx=14, pady=(8, 14))
        table_card.rowconfigure(1, weight=1)
        table_card.columnconfigure(0, weight=1)
        header = ttk.Frame(table_card, style="PanelBody.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Leaderboard results", style="SectionTitle.TLabel").grid(row=0, column=0, sticky="w")
        row_actions = ttk.Frame(header, style="PanelBody.TFrame")
        row_actions.grid(row=0, column=1, sticky="e")
        ttk.Button(row_actions, text="Copy Wallet", command=self.copy_selected_leaderboard_wallet).pack(side="left", padx=(0, 6))
        ttk.Button(row_actions, text="Copy User", command=self.copy_selected_leaderboard_user).pack(side="left", padx=(0, 6))
        ttk.Button(row_actions, text="Track Wallet", command=self.track_selected_leaderboard_wallet).pack(side="left", padx=(0, 6))
        ttk.Button(row_actions, text="Follow for Copy Trading", command=self.follow_selected_leaderboard_for_copy_trading).pack(side="left")

        table_wrap = ttk.Frame(table_card, style="PanelBody.TFrame")
        table_wrap.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        cols = ("rank", "user", "wallet", "pnl", "volume", "roi", "trades", "mdd_usd", "mdd_pct", "source")
        self._leaderboard_row_by_iid: Dict[str, Dict[str, Any]] = {}
        self.leaderboard_tree = ttk.Treeview(table_wrap, columns=cols, show="headings", height=16)
        headings = {
            "rank": "Rank",
            "user": "User",
            "wallet": "Wallet",
            "pnl": "PnL",
            "volume": "Volume",
            "roi": "ROI %",
            "trades": "Trades",
            "mdd_usd": "MDD USD",
            "mdd_pct": "MDD %",
            "source": "MDD source",
        }
        widths = {
            "rank": 70,
            "user": 320,
            "wallet": 360,
            "pnl": 110,
            "volume": 120,
            "roi": 90,
            "trades": 80,
            "mdd_usd": 110,
            "mdd_pct": 90,
            "source": 140,
        }
        for col in cols:
            self.leaderboard_tree.heading(col, text=headings[col])
            anchor = "e" if col in {"rank", "pnl", "volume", "roi", "trades", "mdd_usd", "mdd_pct"} else "w"
            self.leaderboard_tree.column(col, width=widths[col], minwidth=60, anchor=anchor, stretch=col in {"user", "wallet", "source"})
        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.leaderboard_tree.yview)
        xscroll = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.leaderboard_tree.xview)
        self.leaderboard_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.leaderboard_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.leaderboard_tree.bind("<Button-3>", self._show_leaderboard_context_menu)
        self.leaderboard_tree.bind("<Control-Button-1>", self._show_leaderboard_context_menu)
        self.leaderboard_tree.bind("<Double-1>", lambda _event: self.copy_selected_leaderboard_wallet())

        self.leaderboard_menu = tk.Menu(self, tearoff=0)
        self.leaderboard_menu.add_command(label="Copy Wallet", command=self.copy_selected_leaderboard_wallet)
        self.leaderboard_menu.add_command(label="Copy User", command=self.copy_selected_leaderboard_user)
        self.leaderboard_menu.add_separator()
        self.leaderboard_menu.add_command(label="Track Wallet", command=self.track_selected_leaderboard_wallet)
        self.leaderboard_menu.add_command(label="Follow for Copy Trading", command=self.follow_selected_leaderboard_for_copy_trading)

        progress = ttk.Frame(table_card, style="PanelBody.TFrame")
        progress.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        progress.columnconfigure(0, weight=1)
        self.lb_progress_var = tk.DoubleVar(value=0.0)
        self.lb_progress_text_var = tk.StringVar(value="Idle")
        self.lb_progress_bar = ttk.Progressbar(progress, variable=self.lb_progress_var, maximum=100)
        self.lb_progress_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress, textvariable=self.lb_progress_text_var, style="Muted.TLabel", width=18, anchor="e").grid(
            row=0,
            column=1,
            sticky="e",
            padx=(10, 0),
        )

        self.lb_status_var = tk.StringVar(value="Ready. Set Returned/Scanned and load Polymarket ROI rankings.")
        ttk.Label(table_card, textvariable=self.lb_status_var, style="Muted.TLabel", wraplength=1160).grid(row=3, column=0, sticky="ew", pady=(8, 0))

    @staticmethod
    def _leaderboard_sort_value(label: str) -> str:
        normalized = str(label or "").strip().lower()
        return {
            "roi %": "roi_pct",
            "pnl usd": "pnl_usd",
            "volume usd": "volume_usd",
            "mdd %": "mdd_pct",
            "mdd usd": "mdd_usd",
        }.get(normalized, "roi_pct")

    @staticmethod
    def _leaderboard_direction_value(label: str) -> str:
        return "ASC" if str(label or "").strip().lower().startswith("low") else "DESC"

    @staticmethod
    def _leaderboard_mdd_mode_value(label: str) -> str:
        return "mark_replay" if "replay" in str(label or "").strip().lower() else "fast"

    @staticmethod
    def _format_table_number(value: Any, *, decimals: int = 2, suffix: str = "") -> str:
        number = safe_float(value, None)
        if number is None:
            return "-"
        return f"{number:,.{decimals}f}{suffix}"

    def _show_leaderboard_context_menu(self, event) -> None:
        row_id = self.leaderboard_tree.identify_row(event.y)
        if row_id:
            self.leaderboard_tree.selection_set(row_id)
            self.leaderboard_menu.tk_popup(event.x_root, event.y_root)
        self.leaderboard_menu.grab_release()

    def _selected_leaderboard_row(self) -> Optional[Dict[str, Any]]:
        selection = self.leaderboard_tree.selection()
        if not selection:
            return None
        iid = str(selection[0])
        row = getattr(self, "_leaderboard_row_by_iid", {}).get(iid)
        if row:
            return dict(row)
        values = self.leaderboard_tree.item(selection[0], "values")
        if not values:
            return None
        return {
            "rank": values[0] if len(values) > 0 else "",
            "display_name": values[1] if len(values) > 1 else "",
            "wallet": values[2] if len(values) > 2 else "",
            "pnl_usd": values[3] if len(values) > 3 else "",
            "volume_usd": values[4] if len(values) > 4 else "",
            "roi_pct": values[5] if len(values) > 5 else "",
        }

    def _selected_leaderboard_wallet(self) -> Optional[str]:
        row = self._selected_leaderboard_row()
        if not row:
            messagebox.showinfo("Polymarket analytics", "Select a leaderboard row first.")
            return None
        wallet = normalize_wallet(row.get("wallet"))
        if not wallet:
            messagebox.showerror("Polymarket analytics", "Selected row does not contain a valid wallet address.")
            return None
        return wallet

    def _selected_leaderboard_display_name(self) -> str:
        row = self._selected_leaderboard_row() or {}
        name = str(row.get("display_name") or row.get("user") or "").strip()
        return "" if name == "-" else name

    def _copy_text_to_clipboard(self, text: str, label: str) -> bool:
        value = str(text or "").strip()
        if not value or value == "-":
            messagebox.showinfo("Polymarket analytics", f"No {label} value is available for the selected row.")
            return False
        self.clipboard_clear()
        self.clipboard_append(value)
        try:
            self.update_idletasks()
        except Exception:
            pass
        message = f"Copied {label}: {value}"
        if hasattr(self, "lb_status_var"):
            self.lb_status_var.set(message)
        if hasattr(self, "status_var"):
            self.status_var.set(message)
        return True

    def copy_selected_leaderboard_wallet(self) -> None:
        wallet = self._selected_leaderboard_wallet()
        if wallet:
            self._copy_text_to_clipboard(wallet, "wallet")

    def copy_selected_leaderboard_user(self) -> None:
        row = self._selected_leaderboard_row()
        if not row:
            messagebox.showinfo("Polymarket analytics", "Select a leaderboard row first.")
            return
        user = self._selected_leaderboard_display_name() or str(row.get("wallet") or "").strip()
        self._copy_text_to_clipboard(user, "user")

    def _ensure_wallet_watch_from_leaderboard(self, wallet: str, display_name: str = "", *, persist: bool = True) -> bool:
        wallet = wallet.lower().strip()
        if not is_wallet_address(wallet):
            messagebox.showerror("Polymarket analytics", "Selected row does not contain a valid wallet address.")
            return False
        if any(str(w.wallet).lower() == wallet for w in self.cfg.wallets):
            return False
        self.cfg.wallets.append(WalletWatch(wallet=wallet, display_name=display_name, enabled=True))
        if persist:
            save_config(self.cfg)
        self._refresh_wallet_table()
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("log", f"Added wallet watch from leaderboard: {display_name or wallet} ({wallet})"))
        return True

    def track_selected_leaderboard_wallet(self) -> None:
        wallet = self._selected_leaderboard_wallet()
        if not wallet:
            return
        name = self._selected_leaderboard_display_name()
        display_name = name if name and name.lower() != wallet else ""
        added = self._ensure_wallet_watch_from_leaderboard(wallet, display_name)
        if added:
            message = f"Tracking wallet: {wallet}"
        else:
            message = f"Wallet is already tracked: {wallet}"
        self.lb_status_var.set(message)
        self.status_var.set(message)

    def follow_selected_leaderboard_for_copy_trading(self) -> None:
        wallet = self._selected_leaderboard_wallet()
        if not wallet:
            return
        name = self._selected_leaderboard_display_name()
        display_name = name if name and name.lower() != wallet else ""
        follow_wallets = self._copy_follow_wallets_from_text() if hasattr(self, "ct_follow_var") else self.cfg.copytrading.normalized_follow_wallets()
        if follow_wallets is None:
            return
        tracked = self._ensure_wallet_watch_from_leaderboard(wallet, display_name, persist=False)
        added_follow = wallet not in follow_wallets
        if added_follow:
            follow_wallets.append(wallet)
        self.cfg.copytrading.follow_wallet = follow_wallets[0] if follow_wallets else ""
        self.cfg.copytrading.follow_wallets = follow_wallets
        if hasattr(self, "ct_follow_var"):
            self.ct_follow_var.set(", ".join(follow_wallets))
        save_config(self.cfg)
        if hasattr(self, "ui_queue"):
            self.ui_queue.put(("log", f"Added leaderboard wallet to copy-trading follow list: {wallet}"))
        details = []
        if tracked:
            details.append("tracked")
        if added_follow:
            details.append("added to copy trading")
        suffix = ", ".join(details) if details else "already configured"
        message = f"Wallet {suffix}: {wallet}. Copy trading uses future tracked activity when tracking and copy trading are enabled."
        self.lb_status_var.set(message)
        self.status_var.set(message)

    def _polymarket_leaderboard_params(self) -> Dict[str, List[str]]:
        sort_value = App._leaderboard_sort_value(self.lb_sort_var.get())
        direction_value = App._leaderboard_direction_value(self.lb_direction_var.get())
        params: Dict[str, List[str]] = {
            "sort": [sort_value],
            "direction": [direction_value],
            "period": [str(self.lb_period_var.get() or "All").strip().upper()],
            "category": [str(self.lb_category_var.get() or "OVERALL").strip().upper()],
            "limit": [str(self.lb_limit_var.get() or "1000").strip()],
            "scan_limit": [str(self.lb_scan_limit_var.get() or "1000").strip()],
            "compute_mdd": ["true" if bool(self.lb_compute_mdd_var.get()) else "false"],
            "mdd_mode": [App._leaderboard_mdd_mode_value(self.lb_mdd_mode_var.get())],
            "mdd_scan_limit": [str(self.lb_mdd_scan_limit_var.get() or "100").strip()],
        }
        fast_scan_var = getattr(self, "lb_fast_scan_var", None)
        fast_scan = bool(fast_scan_var.get()) if fast_scan_var is not None else False
        params["fast_scan"] = ["true" if fast_scan else "false"]
        params["scan_concurrency"] = ["6" if fast_scan else "1"]
        params["mdd_concurrency"] = ["3" if fast_scan else "1"]
        params["mdd_stop_on_limit"] = ["true" if fast_scan and sort_value == "roi_pct" and direction_value == "DESC" else "false"]
        optional = {
            "min_roi_pct": self.lb_min_roi_var.get(),
            "max_roi_pct": self.lb_max_roi_var.get(),
            "min_mdd_pct": self.lb_min_mdd_pct_var.get(),
            "max_mdd_pct": self.lb_max_mdd_pct_var.get(),
        }
        for key, value in optional.items():
            text = str(value or "").strip()
            if text:
                params[key] = [text]
        return params

    def load_fast_polymarket_roi_mdd(self):
        if self._leaderboard_loading:
            return
        self.lb_sort_var.set("ROI %")
        self.lb_direction_var.set("High to low")
        self.lb_period_var.set("All")
        self.lb_category_var.set("OVERALL")
        self.lb_limit_var.set("100")
        self.lb_scan_limit_var.set("5000")
        self.lb_compute_mdd_var.set(True)
        self.lb_fast_scan_var.set(True)
        self.lb_mdd_mode_var.set("Fast public curve")
        self.lb_mdd_scan_limit_var.set("500")
        self.lb_min_mdd_pct_var.set("")
        self.lb_max_mdd_pct_var.set("20")
        self.load_polymarket_leaderboard()

    def load_polymarket_leaderboard(self):
        if self._leaderboard_loading:
            return
        self._leaderboard_loading = True
        self._leaderboard_cancel_event.clear()
        self.lb_load_btn.configure(state="disabled")
        if hasattr(self, "lb_fast_roi_btn"):
            self.lb_fast_roi_btn.configure(state="disabled")
        if hasattr(self, "lb_cancel_btn"):
            self.lb_cancel_btn.configure(state="normal")
        params = self._polymarket_leaderboard_params()
        if hasattr(self, "lb_progress_var"):
            self.lb_progress_var.set(0.0)
        if hasattr(self, "lb_progress_text_var"):
            self.lb_progress_text_var.set("0%")
        self.lb_status_var.set(
            f"Scanning Polymarket public leaderboard rows: returned={params['limit'][0]}, scanned={params['scan_limit'][0]}..."
        )
        self.status_var.set("Loading Polymarket analytics...")
        threading.Thread(target=self._load_polymarket_leaderboard_bg, args=(params,), daemon=True).start()

    def cancel_polymarket_leaderboard_scan(self):
        if not self._leaderboard_loading:
            self.lb_status_var.set("No active Polymarket analytics scan to cancel.")
            return
        self._leaderboard_cancel_event.set()
        if hasattr(self, "lb_cancel_btn"):
            self.lb_cancel_btn.configure(state="disabled")
        self.lb_status_var.set("Cancelling Polymarket analytics scan after the current API request finishes...")
        self.status_var.set("Cancelling Polymarket analytics scan...")
        self.log("[analytics] cancel requested")

    def _load_polymarket_leaderboard_bg(self, params: Dict[str, List[str]]) -> None:
        try:
            from web_api import polymarket_leaderboard_payload

            def progress_callback(progress: Dict[str, Any]) -> None:
                self.ui_queue.put(("polymarket_leaderboard_progress", progress, None))

            payload = polymarket_leaderboard_payload(
                params,
                cancel_check=self._leaderboard_cancel_event.is_set,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            self.ui_queue.put(("polymarket_leaderboard_error", str(exc), None))
            return
        self.ui_queue.put(("polymarket_leaderboard", payload, None))

    def _handle_polymarket_leaderboard_progress(self, progress: Dict[str, Any]) -> None:
        percent = max(0.0, min(safe_float(progress.get("percent"), 0.0) or 0.0, 100.0))
        phase = str(progress.get("phase") or "leaderboard").replace("_", " ").title()
        scanned = int(safe_float(progress.get("scanned"), 0) or 0)
        scan_limit_unlimited = bool(progress.get("scan_limit_unlimited"))
        scan_limit = int(safe_float(progress.get("scan_limit"), 0) or 0)
        scan_label = "unlimited" if scan_limit_unlimited else str(scan_limit)
        filtered = int(safe_float(progress.get("filtered"), 0) or 0)
        mdd_attempted = int(safe_float(progress.get("mdd_attempted"), 0) or 0)
        mdd_computed = int(safe_float(progress.get("mdd_computed"), 0) or 0)
        mdd_total = int(safe_float(progress.get("mdd_total"), 0) or 0)

        if hasattr(self, "lb_progress_var"):
            self.lb_progress_var.set(percent)
        if hasattr(self, "lb_progress_text_var"):
            self.lb_progress_text_var.set(f"{percent:.0f}%")
        if hasattr(self, "lb_scanned_metric_var"):
            self.lb_scanned_metric_var.set(str(scanned))
        if hasattr(self, "lb_mdd_metric_var"):
            self.lb_mdd_metric_var.set(str(mdd_computed))

        message = str(progress.get("message") or "").strip()
        if not message:
            if phase.lower() == "mdd":
                message = f"Computing MDD {mdd_attempted}/{mdd_total}; scanned {scanned}/{scan_label} leaderboard rows."
            else:
                message = f"Scanning leaderboard rows {scanned}/{scan_label}."
        if phase.lower() == "mdd":
            extra = f" Filtered candidates: {filtered}. Successful MDD: {mdd_computed}."
        else:
            extra = ""
        self.lb_status_var.set(f"{phase}: {percent:.0f}% - {message}{extra}")
        self.status_var.set(f"Polymarket analytics {percent:.0f}%")

    def _refresh_polymarket_leaderboard_table(self, payload: Dict[str, Any]) -> None:
        self._last_leaderboard_payload = payload
        self._leaderboard_row_by_iid = {}
        for iid in self.leaderboard_tree.get_children():
            self.leaderboard_tree.delete(iid)

        rows = list(payload.get("rows") or [])
        for index, row in enumerate(rows):
            iid = f"leaderboard-{index}"
            self._leaderboard_row_by_iid[iid] = dict(row)
            mdd_source = "-"
            if row.get("mdd_available"):
                mdd_source = str(
                    row.get("mdd_accounting_status")
                    or row.get("mdd_mark_replay_status")
                    or row.get("mdd_method")
                    or "fast"
                )
            self.leaderboard_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.get("rank") or index + 1,
                    row.get("display_name") or "-",
                    str(row.get("wallet") or "-"),
                    App._format_table_number(row.get("pnl_usd"), decimals=2),
                    App._format_table_number(row.get("volume_usd"), decimals=2),
                    App._format_table_number(row.get("roi_pct"), decimals=2, suffix="%"),
                    row.get("trade_count") or "-",
                    App._format_table_number(row.get("mdd_usd"), decimals=2) if row.get("mdd_available") else "-",
                    App._format_table_number(row.get("mdd_pct"), decimals=2, suffix="%") if row.get("mdd_available") else "-",
                    mdd_source,
                ),
            )

        counts = payload.get("counts") or {}
        self.lb_returned_metric_var.set(str(counts.get("returned", len(rows))))
        self.lb_scanned_metric_var.set(str(counts.get("scanned", 0)))
        self.lb_mdd_metric_var.set(str(counts.get("mdd_computed", 0)))
        roi_values = [safe_float(row.get("roi_pct"), None) for row in rows]
        roi_values = [value for value in roi_values if value is not None]
        self.lb_best_roi_metric_var.set(App._format_table_number(max(roi_values), decimals=2, suffix="%") if roi_values else "-")

        warnings = payload.get("warnings") or []
        cancelled = bool(payload.get("cancelled"))
        warning_text = f" Warning: {warnings[0]}" if warnings else ""
        status_prefix = "Cancelled. Loaded partial" if cancelled else "Loaded"
        if hasattr(self, "lb_progress_var"):
            self.lb_progress_var.set(self.lb_progress_var.get() if cancelled else 100.0)
        if hasattr(self, "lb_progress_text_var"):
            self.lb_progress_text_var.set("Cancelled" if cancelled else "100%")
        self.lb_status_var.set(
            f"{status_prefix} {counts.get('returned', len(rows))} rows from {counts.get('scanned', 0)} scanned. "
            f"Source: {payload.get('source', 'polymarket')}.{warning_text}"
        )
        self.status_var.set("Polymarket analytics cancelled." if cancelled else "Polymarket analytics loaded.")
        self.log(
            f"[analytics] {'cancelled with' if cancelled else 'loaded'} {counts.get('returned', len(rows))} rows "
            f"from {counts.get('scanned', 0)} scanned "
            f"sort={payload.get('sort')} direction={payload.get('direction')}"
        )

    def _handle_polymarket_leaderboard_error(self, message: str) -> None:
        if hasattr(self, "lb_progress_text_var"):
            self.lb_progress_text_var.set("Failed")
        self.lb_status_var.set(f"Polymarket analytics failed: {message}")
        self.status_var.set("Polymarket analytics failed.")
        self.log(f"[analytics] leaderboard error: {message}")
        messagebox.showerror("Polymarket analytics", message)

    def export_polymarket_leaderboard(self):
        payload = self._last_leaderboard_payload or {}
        rows = list(payload.get("rows") or [])
        if not rows:
            messagebox.showinfo("Polymarket analytics", "Load leaderboard rows before exporting.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Polymarket leaderboard",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="polymarket-top-roi.csv",
        )
        if not path:
            return
        fields = [
            "rank",
            "display_name",
            "wallet",
            "pnl_usd",
            "volume_usd",
            "roi_pct",
            "trade_count",
            "mdd_usd",
            "mdd_pct",
            "mdd_method",
            "mdd_pct_basis",
            "mdd_audit_cache_key",
        ]
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({field: row.get(field) for field in fields})
        except Exception as exc:
            messagebox.showerror("Polymarket analytics", f"Could not export CSV: {exc}")
            return
        self.lb_status_var.set(f"Exported {len(rows)} rows to {path}.")
        self.log(f"[analytics] exported {len(rows)} rows to {path}")

    def _build_copy_tab(self):
        frm = self.tab_copy

        top = ttk.Frame(frm)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Geoblock status:").grid(row=0, column=0, sticky="w")
        self.geo_var = tk.StringVar(value="Unknown (check)")
        ttk.Label(top, textvariable=self.geo_var).grid(row=0, column=1, sticky="w")
        ttk.Button(top, text="Check Geoblock", command=self.do_geoblock_check).grid(row=0, column=2, padx=5)

        # Copy settings form
        form = ttk.Labelframe(frm, text="Copy trading settings (default = SIM)")
        form.pack(fill="x", padx=10, pady=10)

        self.ct_enabled_var = tk.BooleanVar(value=self.cfg.copytrading.enabled)
        ttk.Checkbutton(form, text="Enable copy trading", variable=self.ct_enabled_var).grid(row=0, column=0, sticky="w", padx=5, pady=5)

        self.ct_live_var = tk.BooleanVar(value=self.cfg.copytrading.live)
        ttk.Checkbutton(form, text="LIVE mode (places real orders)", variable=self.ct_live_var).grid(row=0, column=1, sticky="w", padx=5, pady=5)

        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        ttk.Label(form, text=f"Follow identities ({activity_identity_hint(market_id)}):").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.ct_follow_var = tk.StringVar(value=", ".join(self.cfg.copytrading.normalized_follow_wallets()))
        self.ct_follow_combo = ttk.Combobox(form, textvariable=self.ct_follow_var, values=self._wallet_choices(), width=50)
        self.ct_follow_combo.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(form, text="Copy % (0..100):").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.ct_scale_var = tk.StringVar(value=f"{max(0.0, min(float(self.cfg.copytrading.scale), 1.0)) * 100.0:g}")
        ttk.Entry(form, textvariable=self.ct_scale_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(form, text="Max USDC / trade:").grid(row=3, column=0, sticky="e", padx=5, pady=5)
        self.ct_max_var = tk.StringVar(value=str(self.cfg.copytrading.max_usdc_per_trade))
        ttk.Entry(form, textvariable=self.ct_max_var, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(form, text="Slippage (0..1):").grid(row=4, column=0, sticky="e", padx=5, pady=5)
        self.ct_slip_var = tk.StringVar(value=str(self.cfg.copytrading.slippage))
        ttk.Entry(form, textvariable=self.ct_slip_var, width=10).grid(row=4, column=1, sticky="w", padx=5, pady=5)

        self.ct_allow_sells_var = tk.BooleanVar(value=self.cfg.copytrading.allow_sells)
        ttk.Checkbutton(form, text="Allow copying SELL trades (riskier)", variable=self.ct_allow_sells_var).grid(row=5, column=1, sticky="w", padx=5, pady=5)

        self.ct_conflict_guard_var = tk.BooleanVar(value=self.cfg.copytrading.conflict_guard)
        ttk.Checkbutton(
            form,
            text="Conflict guard: skip duplicate/opposite same-token copies",
            variable=self.ct_conflict_guard_var,
        ).grid(row=6, column=1, sticky="w", padx=5, pady=5)

        ttk.Button(form, text="Save settings", command=self.save_copy_settings).grid(row=7, column=1, sticky="w", padx=5, pady=10)

        hint = ttk.Label(frm, text="LIVE mode requires PRIVATE_KEY (+ optional FUNDER_ADDRESS/SIGNATURE_TYPE) set in .env or environment variables.")
        hint.pack(anchor="w", padx=10)

    # ------------------ Logging ------------------

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.insert("end", line)
        self.log_text.see("end")

    # ------------------ Market fetch / alerts ------------------

    def _set_market_loaded(self, market: Dict[str, Any], fallback_slug: str = ""):
        self._market_loaded = market
        title = market.get("question") or market.get("title") or fallback_slug or "Unknown market"
        slug = market.get("slug") or fallback_slug
        if slug:
            self.market_info_var.set(f"Loaded: {title} (slug: {slug})")
        else:
            self.market_info_var.set(f"Loaded: {title}")
        self._market_outcomes = self._get_polymarket_adapter().parse_market_outcomes(market)

        self.outcome_list.delete(0, "end")
        for o in self._market_outcomes:
            price_str = f"{o.price:.3f}" if isinstance(o.price, float) else "?"
            self.outcome_list.insert("end", f"{o.outcome}  |  token {o.token_id[:10]}...  |  market price {price_str}")

        self.status_var.set("Market loaded. Select an outcome to create an alert.")
        self._selected_token_id = None
        self._selected_alert_market_id = "polymarket"
        App._set_paper_contract_selection(self, "polymarket", "")

    def _set_adapter_event_loaded(
        self,
        adapter: MarketAdapter,
        event: MarketEvent,
        contracts: List[MarketContract],
    ) -> None:
        self._market_loaded = {
            "market_id": adapter.market_id,
            "event_id": event.event_id,
            "title": event.title,
            "status": event.status,
            "url": event.url,
        }
        self._market_outcomes = list(contracts)
        self._selected_token_id = None
        self._selected_alert_market_id = adapter.market_id

        status = f", {event.status}" if event.status else ""
        self.market_info_var.set(
            f"Loaded: {event.title} ({adapter.display_name}, event {event.event_id}{status})"
        )

        self.outcome_list.delete(0, "end")
        for contract in self._market_outcomes:
            title = contract.outcome or contract.title or contract.contract_id
            contract_id = contract.contract_id
            short_id = contract_id if len(contract_id) <= 28 else f"{contract_id[:25]}..."
            status_text = f"  |  {contract.status}" if contract.status else ""
            self.outcome_list.insert("end", f"{title}  |  contract {short_id}{status_text}")

        self.status_var.set("Market loaded. Select a contract to create an adapter-backed alert.")
        App._set_paper_contract_selection(self, adapter.market_id, "")

    def _select_market_from_event(self, event: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
        markets = list(event.get("markets") or [])
        if not markets:
            return None, False

        active = [m for m in markets if m.get("active") or not m.get("closed")]
        if len(active) == 1:
            return active[0], True
        if len(markets) == 1:
            return markets[0], True

        ordered = active + [m for m in markets if m not in active]

        win = tk.Toplevel(self)
        win.title("Select market")
        win.geometry("820x360")
        self._apply_window_icon(win)
        palette = self._palette
        win.configure(background=palette["bg"])

        lb = tk.Listbox(win)
        lb.configure(
            bg=palette["field_bg"],
            fg=palette["field_fg"],
            selectbackground=palette["select_bg"],
            selectforeground=palette["select_fg"],
            highlightbackground=palette["border"],
            highlightcolor=palette["border"],
        )
        lb.pack(fill="both", expand=True, padx=10, pady=10)

        for m in ordered:
            question = m.get("question") or m.get("title") or m.get("slug") or m.get("id") or "Unknown"
            status = "closed" if m.get("closed") else "active" if m.get("active") else "inactive"
            lb.insert("end", f"{question} [{status}]")

        result: Dict[str, Any] = {}

        def load_selected():
            sel = lb.curselection()
            if not sel:
                return
            result["market"] = ordered[sel[0]]
            win.destroy()

        ttk.Button(win, text="Load market", command=load_selected).pack(pady=(0, 10))
        self.status_var.set("Select a market from the event.")
        win.grab_set()
        self.wait_window(win)
        return result.get("market") if result else None, True

    def _select_adapter_event(self, events: List[MarketEvent]) -> Optional[MarketEvent]:
        if not events:
            return None
        if len(events) == 1:
            return events[0]

        win = tk.Toplevel(self)
        win.title("Select market event")
        win.geometry("820x360")
        self._apply_window_icon(win)
        palette = self._palette
        win.configure(background=palette["bg"])

        lb = tk.Listbox(win)
        lb.configure(
            bg=palette["field_bg"],
            fg=palette["field_fg"],
            selectbackground=palette["select_bg"],
            selectforeground=palette["select_fg"],
            highlightbackground=palette["border"],
            highlightcolor=palette["border"],
        )
        lb.pack(fill="both", expand=True, padx=10, pady=10)

        for event in events:
            status = f" [{event.status}]" if event.status else ""
            lb.insert("end", f"{event.title}{status}  |  {event.event_id}")

        result: Dict[str, MarketEvent] = {}

        def load_selected():
            sel = lb.curselection()
            if not sel:
                return
            result["event"] = events[sel[0]]
            win.destroy()

        ttk.Button(win, text="Load event", command=load_selected).pack(pady=(0, 10))
        self.status_var.set("Select an event from the selected adapter.")
        win.grab_set()
        self.wait_window(win)
        return result.get("event")

    def fetch_market(self):
        raw = self.market_entry.get()
        if not str(raw or "").strip():
            messagebox.showerror("Error", "Enter a market search term, event id, slug, or URL.")
            return

        adapter = self._get_selected_market_adapter()
        if not App._require_market_enabled(self, adapter.market_id, "market search"):
            return
        if adapter.market_id == "polymarket":
            self._fetch_polymarket_market(raw)
            return
        self._fetch_adapter_market(adapter, raw)

    def _fetch_polymarket_market(self, raw: str):
        slug = extract_slug(raw)
        if not slug:
            messagebox.showerror("Error", "Enter a market slug or a Polymarket URL.")
            return
        self.status_var.set("Fetching market from Polymarket adapter...")
        self.update_idletasks()
        adapter = self._get_polymarket_adapter()

        try:
            if slug.isdigit():
                m = adapter.get_market_by_id(slug)
                if m:
                    self._set_market_loaded(m)
                    return
                ev = adapter.get_event_by_id(slug)
                if ev:
                    m, has_markets = self._select_market_from_event(ev)
                    if not has_markets:
                        messagebox.showerror("Not found", "Event has no markets.")
                        self.status_var.set("No markets found for event.")
                        return
                    if not m:
                        self.status_var.set("Market selection canceled.")
                        return
                    self._set_market_loaded(m, fallback_slug=str(ev.get("slug") or ""))
                    return
                messagebox.showerror(
                    "Not found",
                    "No market/event found for that numeric id. "
                    "If this is a Polymarket ?tid value, paste the full URL or slug instead.",
                )
                self.status_var.set("Market not found.")
                return

            m = adapter.get_market_by_slug(slug)
            if m:
                self._set_market_loaded(m, fallback_slug=slug)
                return

            ev = adapter.get_event_by_slug(slug)
            if ev:
                m, has_markets = self._select_market_from_event(ev)
                if not has_markets:
                    messagebox.showerror("Not found", "Event has no markets.")
                    self.status_var.set("No markets found for event.")
                    return
                if not m:
                    self.status_var.set("Market selection canceled.")
                    return
                self._set_market_loaded(m, fallback_slug=str(ev.get("slug") or slug))
                return

            messagebox.showerror("Not found", f"Could not fetch market or event for: {slug}")
            self.status_var.set("Market not found.")

        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.status_var.set("Error fetching market.")

    def _fetch_adapter_market(self, adapter: MarketAdapter, raw: str) -> None:
        if not adapter.capabilities.event_listing:
            messagebox.showinfo(
                "Unsupported market",
                f"{adapter.display_name} does not support market/event listing in this app.",
            )
            self.status_var.set(f"{adapter.display_name} market listing is unsupported.")
            return

        query = extract_slug(raw) or str(raw or "").strip()
        self.status_var.set(f"Searching {adapter.display_name}...")
        self.update_idletasks()

        try:
            events = adapter.list_events(query, limit=25)
            if not events:
                try:
                    contracts = adapter.list_contracts(query)
                except Exception:
                    contracts = []
                if contracts:
                    event = MarketEvent(
                        market_id=adapter.market_id,
                        event_id=query,
                        title=query,
                    )
                    self._set_adapter_event_loaded(adapter, event, contracts)
                    return
                messagebox.showerror("Not found", f"No {adapter.display_name} events found for: {query}")
                self.status_var.set("Market not found.")
                return

            event = self._select_adapter_event(events)
            if event is None:
                self.status_var.set("Market selection canceled.")
                return

            contracts = adapter.list_contracts(event.event_id)
            if not contracts:
                messagebox.showerror("Not found", "Event has no contracts.")
                self.status_var.set("No contracts found for event.")
                return
            self._set_adapter_event_loaded(adapter, event, contracts)
        except UnsupportedFeatureError as e:
            messagebox.showinfo("Unsupported market", str(e))
            self.status_var.set(str(e))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.status_var.set("Error fetching market.")

    def _on_outcome_selected(self):
        idxs = self.outcome_list.curselection()
        if not idxs:
            return
        i = idxs[0]
        if i >= len(self._market_outcomes):
            return
        outcome = self._market_outcomes[i]
        tok = str(getattr(outcome, "token_id", "") or getattr(outcome, "contract_id", "") or "")
        if not tok:
            return
        self._selected_token_id = tok
        self._selected_alert_market_id = str(
            getattr(outcome, "market_id", "") or self.cfg.selected_market_id or "polymarket"
        ).strip().lower()
        # Pre-fill label if blank
        if not self.alert_label_entry.get().strip():
            label = str(getattr(outcome, "outcome", "") or getattr(outcome, "title", "") or tok)
            self.alert_label_entry.insert(0, label)
        noun = "token_id" if self._selected_alert_market_id == "polymarket" else "contract_id"
        self.status_var.set(f"Selected {noun}: {tok}")
        App._set_paper_contract_selection(self, self._selected_alert_market_id, tok)

    def add_alert(self):
        token_id = self._selected_token_id
        if not token_id:
            messagebox.showerror("Error", "Select a market outcome first.")
            return
        market_id = str(self._selected_alert_market_id or self.cfg.selected_market_id or "polymarket").strip().lower()
        if not App._require_market_enabled(self, market_id, "price alerts"):
            return
        adapter = self.adapter_registry.create(
            market_id,
            self.cfg.markets.get(market_id).settings if self.cfg.markets.get(market_id) else {},
        )
        if not adapter.capabilities.alerts:
            messagebox.showinfo(
                "Unsupported market",
                f"{adapter.display_name} does not support price alerts in this app.",
            )
            return

        label = self.alert_label_entry.get().strip() or f"Alert {token_id[:8]}"
        thr = safe_float(self.alert_threshold_entry.get().strip())
        if thr is None or thr < 0 or thr > 1:
            messagebox.showerror("Error", "Threshold must be a number between 0 and 1.")
            return

        direction = self.alert_dir_var.get()
        source = self.alert_src_var.get()
        once = bool(self.alert_once_var.get())

        a = PriceAlert(
            token_id=token_id,
            label=label,
            direction=direction,  # type: ignore
            threshold=float(thr),
            source=source,  # type: ignore
            once=once,
            enabled=True,
            market_id=market_id,
        )
        self.cfg.alerts.append(a)
        save_config(self.cfg)
        self._refresh_alert_table()

        if market_id == "polymarket":
            self.market_ws.subscribe([token_id])

        self.status_var.set("Alert added.")
        self.ui_queue.put(
            ("log", f"Added alert: {label} {direction} {thr} ({source}) on {market_id}:{token_id}")
        )

    def _refresh_alert_table(self):
        for iid in self.alert_tree.get_children():
            self.alert_tree.delete(iid)
        for a in self.cfg.alerts:
            last_val = "" if a.last_value is None else f"{a.last_value:.3f}"
            self.alert_tree.insert(
                "",
                "end",
                iid=a.id,
                values=(
                    self._alert_market_id(a),
                    a.label,
                    a.token_id,
                    a.direction,
                    f"{a.threshold:.3f}",
                    a.source,
                    "yes" if a.enabled else "no",
                    "yes" if a.triggered else "no",
                    last_val,
                ),
            )

    def _selected_alert_id(self) -> Optional[str]:
        sel = self.alert_tree.selection()
        return sel[0] if sel else None

    def toggle_selected_alert(self):
        aid = self._selected_alert_id()
        if not aid:
            return
        for a in self.cfg.alerts:
            if a.id == aid:
                a.enabled = not a.enabled
                if a.enabled and self._alert_market_id(a) == "polymarket":
                    self.market_ws.subscribe([a.token_id])
                else:
                    # we don't unsubscribe automatically because another alert might use same token
                    pass
                save_config(self.cfg)
                self._refresh_alert_table()
                self.ui_queue.put(("log", f"Toggled alert {a.label} -> {'enabled' if a.enabled else 'disabled'}"))
                return

    def delete_selected_alert(self):
        aid = self._selected_alert_id()
        if not aid:
            return
        self.cfg.alerts = [a for a in self.cfg.alerts if a.id != aid]
        save_config(self.cfg)
        self._refresh_alert_table()
        self.ui_queue.put(("log", f"Deleted alert {aid}"))
        # WS unsubscribe: only unsubscribe if no other alert uses token
        # (We'll just resubscribe to current set to keep it simple.)
        self.resubscribe_ws()

    def resubscribe_ws(self):
        ids = self._enabled_polymarket_alert_ids()
        self.market_ws.set_tokens(ids)
        # Reconnect by stopping and restarting client (simple, brute-force)
        self.market_ws.stop()
        self.market_ws = MarketWSClient(
            token_ids=ids,
            on_event=self._on_market_event_bg,
            custom_feature_enabled=False,
            verbose=False,
        )
        if self._background_workers_started:
            self.market_ws.start()
        self.ui_queue.put(("log", f"Resubscribed WS to {len(ids)} tokens."))

    # ------------------ Paper trading ------------------

    def _adapter_for_market(self, market_id: str) -> MarketAdapter:
        normalized = str(market_id or "polymarket").strip().lower()
        market_cfg = self.cfg.markets.get(normalized)
        settings = market_cfg.settings if market_cfg else {}
        return self.adapter_registry.create(normalized, settings)

    def _set_paper_contract_selection(self, market_id: str, contract_id: str) -> None:
        if not hasattr(self, "paper_market_var"):
            return
        normalized = str(market_id or self.cfg.selected_market_id or "polymarket").strip().lower()
        self.paper_market_var.set(normalized)
        self.paper_contract_var.set(str(contract_id or ""))
        label = f"Selected contract: {normalized}:{contract_id}" if contract_id else "No contract selected."
        self.paper_selected_var.set(label)
        App._refresh_paper_market_state(self)

    def _refresh_paper_market_state(self) -> None:
        if not hasattr(self, "paper_submit_btn"):
            return
        market_id = str(self.paper_market_var.get() or self.cfg.selected_market_id or "polymarket").strip().lower()
        if not market_config_enabled(self.cfg, market_id):
            display_name = App._market_display_name_for_id(self, market_id)
            self.paper_status_var.set(
                f"{display_name} is disabled in local market config. Enable it in Market Safety before paper actions."
            )
            self.paper_submit_btn.configure(state="disabled")
            if hasattr(self, "paper_quote_btn"):
                self.paper_quote_btn.configure(state="disabled")
            if hasattr(self, "paper_quote_limit_btn"):
                self.paper_quote_limit_btn.configure(state="disabled")
            return
        try:
            adapter = App._adapter_for_market(self, market_id)
        except Exception as exc:
            self.paper_status_var.set(f"Market adapter unavailable: {exc}")
            self.paper_submit_btn.configure(state="disabled")
            if hasattr(self, "paper_quote_btn"):
                self.paper_quote_btn.configure(state="disabled")
            if hasattr(self, "paper_quote_limit_btn"):
                self.paper_quote_limit_btn.configure(state="disabled")
            return
        quote_supported = bool(adapter.capabilities.price_reading or adapter.capabilities.orderbook_reading)
        if hasattr(self, "paper_quote_btn"):
            self.paper_quote_btn.configure(state="normal" if quote_supported else "disabled")
        if hasattr(self, "paper_quote_limit_btn"):
            self.paper_quote_limit_btn.configure(state="normal" if quote_supported else "disabled")
        if adapter.capabilities.paper_trading:
            suffix = " and quote previews" if quote_supported else ""
            self.paper_status_var.set(f"{adapter.display_name} supports local paper orders{suffix}.")
            self.paper_submit_btn.configure(state="normal")
        elif quote_supported:
            self.paper_status_var.set(
                f"{adapter.display_name} does not support paper trading in this app; quote previews are available."
            )
            self.paper_submit_btn.configure(state="disabled")
        else:
            self.paper_status_var.set(f"{adapter.display_name} does not support paper trading in this app.")
            self.paper_submit_btn.configure(state="disabled")

    def use_selected_contract_for_paper(self) -> None:
        if not self._selected_token_id:
            messagebox.showerror("Error", "Select a market outcome or contract first.")
            return
        App._set_paper_contract_selection(self, self._selected_alert_market_id, self._selected_token_id)

    def _paper_order_from_form(self) -> PaperOrderRequest:
        market_id = str(self.paper_market_var.get() or "").strip().lower()
        contract_id = str(self.paper_contract_var.get() or "").strip()
        side = str(self.paper_side_var.get() or "").strip().upper()
        size = safe_float(self.paper_size_var.get(), None)
        raw_limit = str(self.paper_limit_var.get() or "").strip()
        limit_price = None if raw_limit == "" else safe_float(raw_limit, None)

        if not market_id:
            raise ValueError("Select a market.")
        if not contract_id:
            raise ValueError("Enter a contract id.")
        if side not in {"BUY", "SELL", "BACK", "LAY"}:
            raise ValueError("Side must be BUY, SELL, BACK, or LAY.")
        if size is None or size <= 0:
            raise ValueError("Size must be a positive number.")
        if raw_limit and limit_price is None:
            raise ValueError("Limit must be a number, or blank for adapters that allow market-style paper orders.")

        return PaperOrderRequest(
            market_id=market_id,
            contract_id=contract_id,
            side=side,
            size=float(size),
            limit_price=limit_price,
        )

    def submit_paper_order(self) -> None:
        try:
            order = App._paper_order_from_form(self)
            if not App._require_market_enabled(self, order.market_id, "paper orders"):
                return
            adapter = App._adapter_for_market(self, order.market_id)
            if not adapter.capabilities.paper_trading:
                messagebox.showinfo(
                    "Unsupported market",
                    f"{adapter.display_name} does not support paper trading in this app.",
                )
                App._refresh_paper_market_state(self)
                return
            result = adapter.place_paper_order(order)
        except UnsupportedFeatureError as exc:
            messagebox.showinfo("Unsupported market", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paper order error", str(exc))
            self.paper_status_var.set("Paper order rejected.")
            return

        record = App._record_paper_trade(self, order, result)
        self.paper_status_var.set(record.message)
        self.status_var.set("Paper order recorded.")
        self.ui_queue.put(
            (
                "log",
                f"[paper] {record.market_id}:{record.contract_id} {record.side} "
                f"size={record.size:g} accepted={record.accepted}: {record.message}",
            )
        )

    def preview_paper_order_impact(self) -> None:
        try:
            order = App._paper_order_from_form(self)
            if not App._require_market_enabled(self, order.market_id, "paper order impact preview"):
                return
            impact = App._paper_order_impact(self.cfg.paper_trades, order)
        except Exception as exc:
            messagebox.showerror("Paper impact error", str(exc))
            self.paper_status_var.set("Paper order impact preview failed.")
            return

        message = App._format_paper_order_impact(impact)
        self.paper_status_var.set(message)
        self.status_var.set("Paper order impact previewed.")
        self.ui_queue.put(("log", f"[paper-impact] {order.market_id}:{order.contract_id} {message}"))

    def refresh_paper_quote(self) -> None:
        try:
            market_id, contract_id, adapter, snapshot, orderbook = App._paper_quote_from_form(self)
        except UnsupportedFeatureError as exc:
            messagebox.showinfo("Unsupported market", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except MarketConfigurationError as exc:
            messagebox.showinfo("Market disabled", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Quote error", str(exc))
            self.paper_status_var.set("Quote refresh failed.")
            return

        message = App._format_paper_quote(adapter.display_name, snapshot, orderbook)
        self.paper_status_var.set(message)
        self.status_var.set("Paper quote refreshed.")
        self.ui_queue.put(("log", f"[quote] {market_id}:{contract_id} {message}"))

    def use_quote_limit_for_paper(self) -> None:
        try:
            market_id, contract_id, _adapter, snapshot, orderbook = App._paper_quote_from_form(self)
            side = str(self.paper_side_var.get() or "").strip().upper()
            limit, source = App._quote_limit_for_side(side, snapshot, orderbook)
        except UnsupportedFeatureError as exc:
            messagebox.showinfo("Unsupported market", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except MarketConfigurationError as exc:
            messagebox.showinfo("Market disabled", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Quote limit error", str(exc))
            self.paper_status_var.set("Quote limit update failed.")
            return

        self.paper_limit_var.set(f"{limit:g}")
        message = f"Limit set from {source}: {limit:g}"
        self.paper_status_var.set(message)
        self.status_var.set("Paper limit updated from quote.")
        self.ui_queue.put(("log", f"[quote-limit] {market_id}:{contract_id} {side} {message}"))

    def _paper_quote_from_form(
        self,
    ) -> Tuple[str, str, MarketAdapter, Optional[PriceSnapshot], Optional[OrderBookSnapshot]]:
        market_id = str(self.paper_market_var.get() or "").strip().lower()
        contract_id = str(self.paper_contract_var.get() or "").strip()
        if not market_id:
            raise ValueError("Select a market.")
        if not contract_id:
            raise ValueError("Enter a contract id.")

        if not market_config_enabled(self.cfg, market_id):
            raise MarketConfigurationError(App._market_disabled_message(self, market_id, "quote previews"))

        adapter = App._adapter_for_market(self, market_id)
        if not (adapter.capabilities.price_reading or adapter.capabilities.orderbook_reading):
            raise UnsupportedFeatureError(
                adapter.market_id,
                "price_reading",
                f"{adapter.display_name} does not support quote or orderbook previews in this app.",
            )

        snapshot = adapter.get_price(contract_id) if adapter.capabilities.price_reading else None
        orderbook = adapter.get_orderbook(contract_id) if adapter.capabilities.orderbook_reading else None
        return market_id, contract_id, adapter, snapshot, orderbook

    def preview_live_preflight(self) -> None:
        try:
            order = App._paper_order_from_form(self)
            if not App._require_market_enabled(self, order.market_id, "live preflight preview"):
                return
            adapter = App._adapter_for_market(self, order.market_id)
            if not adapter.capabilities.live_trading:
                raise UnsupportedFeatureError(
                    adapter.market_id,
                    "live_trading",
                    f"{adapter.display_name} does not support live trading in this app.",
                )
            preflight = adapter.preflight_live_order(order, feature_name="live preflight preview")
        except UnsupportedFeatureError as exc:
            messagebox.showinfo("Unsupported market", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except Exception as exc:
            self.paper_status_var.set(f"Live preflight blocked: {exc}")
            self.status_var.set("Live preflight blocked.")
            self.ui_queue.put(("log", f"[preflight] blocked: {exc}"))
            return

        message = App._format_live_preflight(preflight)
        self.paper_status_var.set(message)
        self.status_var.set("Live preflight preview passed.")
        self.ui_queue.put(("log", f"[preflight] {message}"))

    @staticmethod
    def _format_live_preflight(preflight: Dict[str, Any]) -> str:
        preview = str(preflight.get("dry_run_preview") or "Live order preflight passed.")
        notional = preflight.get("approx_notional")
        max_notional = preflight.get("max_notional")
        warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []

        parts = [f"Preflight OK: {preview}"]
        if isinstance(notional, (int, float)):
            parts.append(f"notional~{float(notional):g}")
        if isinstance(max_notional, (int, float)):
            parts.append(f"max_notional={float(max_notional):g}")
        if warnings:
            parts.append("warnings=" + ",".join(str(item) for item in warnings))
        return "; ".join(parts)

    @staticmethod
    def _format_paper_quote(
        display_name: str,
        snapshot: Optional[PriceSnapshot],
        orderbook: Optional[OrderBookSnapshot],
    ) -> str:
        parts = [f"Quote: {display_name}"]
        if snapshot is not None:
            parts.extend(App._format_snapshot_parts(snapshot))
        if orderbook is not None:
            parts.extend(App._format_orderbook_parts(orderbook))
        return "; ".join(parts)

    @staticmethod
    def _format_snapshot_parts(snapshot: PriceSnapshot) -> List[str]:
        values = AdapterPricePoller._snapshot_values(snapshot)
        labels = (
            ("last", values["last_trade"]),
            ("midpoint", values["midpoint"]),
            ("bid", values["best_bid"]),
            ("ask", values["best_ask"]),
        )
        parts = [f"{label}={float(value):g}" for label, value in labels if value is not None]
        if snapshot.source:
            parts.append(f"source={snapshot.source}")
        return parts

    @staticmethod
    def _format_orderbook_parts(orderbook: OrderBookSnapshot) -> List[str]:
        parts = []
        if orderbook.bids:
            bid = orderbook.bids[0]
            parts.append(f"best_bid={bid.price:g}x{bid.size:g}")
        if orderbook.asks:
            ask = orderbook.asks[0]
            parts.append(f"best_ask={ask.price:g}x{ask.size:g}")
        depth = len(orderbook.bids) + len(orderbook.asks)
        if depth:
            parts.append(f"book_levels={depth}")
        return parts

    @staticmethod
    def _quote_limit_for_side(
        side: str,
        snapshot: Optional[PriceSnapshot],
        orderbook: Optional[OrderBookSnapshot],
    ) -> Tuple[float, str]:
        normalized_side = str(side or "").strip().upper()
        if normalized_side not in {"BUY", "SELL", "BACK", "LAY"}:
            raise ValueError("Side must be BUY, SELL, BACK, or LAY.")

        wants_ask = normalized_side in {"BUY", "BACK"}
        if wants_ask and orderbook is not None and orderbook.asks:
            return float(orderbook.asks[0].price), "best_ask"
        if not wants_ask and orderbook is not None and orderbook.bids:
            return float(orderbook.bids[0].price), "best_bid"

        values = AdapterPricePoller._snapshot_values(snapshot) if snapshot is not None else {}
        fallback_order = (
            ("best_ask", "ask"),
            ("midpoint", "midpoint"),
            ("last_trade", "last"),
            ("best_bid", "bid"),
        ) if wants_ask else (
            ("best_bid", "bid"),
            ("midpoint", "midpoint"),
            ("last_trade", "last"),
            ("best_ask", "ask"),
        )
        for key, label in fallback_order:
            value = values.get(key)
            if value is not None:
                return float(value), label
        raise ValueError("No quote price is available for the selected side.")

    def _record_paper_trade(self, order: PaperOrderRequest, result: PaperOrderResult) -> PaperTradeRecord:
        record = PaperTradeRecord(
            market_id=order.market_id,
            contract_id=order.contract_id,
            side=order.side.upper(),
            size=float(order.size),
            limit_price=order.limit_price,
            accepted=bool(result.accepted),
            message=str(result.message),
            filled_size=float(result.filled_size or 0.0),
            average_price=result.average_price,
            raw=dict(result.raw or {}),
        )
        self.cfg.paper_trades.insert(0, record)
        if len(self.cfg.paper_trades) > 200:
            self.cfg.paper_trades = self.cfg.paper_trades[:200]
        save_config(self.cfg)
        App._refresh_paper_trade_table(self)
        return record

    def use_selected_paper_trade(self) -> None:
        try:
            record = App._selected_paper_trade_record(self)
        except Exception as exc:
            messagebox.showerror("Paper history", str(exc))
            self.paper_status_var.set(str(exc))
            return

        market_id = str(record.market_id or self.cfg.selected_market_id or "polymarket").strip().lower()
        contract_id = str(record.contract_id or "").strip()
        side = str(record.side or "").strip().upper()
        size = float(record.size or 0.0)

        self.paper_market_var.set(market_id)
        self.paper_contract_var.set(contract_id)
        self.paper_side_var.set(side)
        self.paper_size_var.set(f"{size:g}")
        self.paper_limit_var.set("" if record.limit_price is None else f"{float(record.limit_price):g}")
        self.paper_selected_var.set(
            f"Selected contract: {market_id}:{contract_id}" if contract_id else "No contract selected."
        )
        App._refresh_paper_market_state(self)

        message = f"Loaded paper history order: {market_id}:{contract_id} {side} size={size:g}"
        self.paper_status_var.set(message)
        self.status_var.set("Paper order loaded into form.")
        self.ui_queue.put(("log", f"[paper] loaded history order {market_id}:{contract_id} {side} size={size:g}"))

    def _selected_paper_trade_record(self) -> PaperTradeRecord:
        if not hasattr(self, "paper_tree"):
            raise ValueError("Paper order history is not available.")
        selected = tuple(self.paper_tree.selection())
        if not selected:
            raise ValueError("Select a paper order history row first.")
        selected_id = str(selected[0])
        for record in self.cfg.paper_trades:
            if record.id == selected_id:
                return record
        raise ValueError("Selected paper order is no longer in history.")

    def use_selected_paper_position(self) -> None:
        try:
            row = App._selected_paper_position_row(self)
        except Exception as exc:
            messagebox.showerror("Paper exposure", str(exc))
            self.paper_status_var.set(str(exc))
            return

        market_id = str(row["market_id"]).strip().lower()
        contract_id = str(row["contract_id"]).strip()
        net_size = float(row["net_size"])
        size = abs(net_size)
        side = App._paper_position_close_side(self.cfg.paper_trades, market_id, contract_id, net_size)

        self.paper_market_var.set(market_id)
        self.paper_contract_var.set(contract_id)
        self.paper_side_var.set(side)
        self.paper_size_var.set(f"{size:g}")
        self.paper_limit_var.set("")
        self.paper_selected_var.set(f"Selected contract: {market_id}:{contract_id}")
        App._refresh_paper_market_state(self)

        message = f"Loaded paper position into form: {market_id}:{contract_id} {side} size={size:g}; limit cleared."
        if not market_config_enabled(self.cfg, market_id):
            message += " Market is disabled in local config."
        self.paper_status_var.set(message)
        self.status_var.set("Paper position loaded into form.")
        self.ui_queue.put(("log", f"[paper] loaded position {market_id}:{contract_id} {side} size={size:g}"))

    def refresh_paper_position_marks(self) -> None:
        rows = App._paper_position_rows(self.cfg.paper_trades)
        if not rows:
            self._paper_position_marks = {}
            App._refresh_paper_position_table(self)
            self.paper_status_var.set("No open paper exposure to mark.")
            self.status_var.set("No paper exposure to mark.")
            return

        marks: Dict[Tuple[str, str], Dict[str, Any]] = {}
        adapter_cache: Dict[str, MarketAdapter] = {}
        problems: List[str] = []
        marked_at = int(time.time())

        for row in rows:
            market_id = str(row["market_id"] or "").strip().lower()
            contract_id = str(row["contract_id"] or "").strip()
            if not market_config_enabled(self.cfg, market_id):
                problems.append(f"{market_id}: disabled")
                continue
            try:
                adapter = adapter_cache.get(market_id)
                if adapter is None:
                    adapter = App._adapter_for_market(self, market_id)
                    adapter_cache[market_id] = adapter
                if not adapter.capabilities.price_reading:
                    problems.append(f"{adapter.display_name}: no price feed")
                    continue
                snapshot = adapter.get_price(contract_id)
                mark_price, source = App._paper_position_mark_price(snapshot, float(row["net_size"]))
                marks[(market_id, contract_id)] = {
                    "mark_price": mark_price,
                    "source": source,
                    "marked_at": marked_at,
                }
            except Exception as exc:
                problems.append(f"{market_id}:{contract_id}: {exc}")

        self._paper_position_marks = marks
        App._refresh_paper_position_table(self)

        message = f"Marked {len(marks)}/{len(rows)} paper positions."
        if problems:
            message += " Skipped: " + "; ".join(problems[:3])
            if len(problems) > 3:
                message += f"; +{len(problems) - 3} more"
        self.paper_status_var.set(message)
        self.status_var.set("Paper exposure marks refreshed.")
        self.ui_queue.put(("log", f"[paper] {message}"))

    def refresh_selected_paper_position_mark(self) -> None:
        try:
            row = App._selected_paper_position_row(self)
            market_id = str(row["market_id"]).strip().lower()
            contract_id = str(row["contract_id"]).strip()
            net_size = float(row["net_size"])
            if not App._require_market_enabled(self, market_id, "paper mark refresh"):
                return
            adapter = App._adapter_for_market(self, market_id)
            if not adapter.capabilities.price_reading:
                raise UnsupportedFeatureError(
                    adapter.market_id,
                    "price_reading",
                    f"{adapter.display_name} does not support paper mark refresh in this app.",
                )
            snapshot = adapter.get_price(contract_id)
            mark_price, source = App._paper_position_mark_price(snapshot, net_size)
        except UnsupportedFeatureError as exc:
            messagebox.showinfo("Unsupported market", str(exc))
            self.paper_status_var.set(str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paper mark error", str(exc))
            self.paper_status_var.set("Selected paper mark refresh failed.")
            return

        current_rows = App._paper_position_rows(self.cfg.paper_trades)
        marks = App._paper_marks_for_rows(getattr(self, "_paper_position_marks", {}) or {}, current_rows)
        marks[(market_id, contract_id)] = {
            "mark_price": mark_price,
            "source": source,
            "marked_at": int(time.time()),
        }
        self._paper_position_marks = marks
        App._refresh_paper_position_table(self)

        message = f"Marked selected paper position: {market_id}:{contract_id} {source}={mark_price:g}"
        self.paper_status_var.set(message)
        self.status_var.set("Selected paper exposure mark refreshed.")
        self.ui_queue.put(("log", f"[paper] {message}"))

    def clear_selected_paper_position_mark(self) -> None:
        try:
            row = App._selected_paper_position_row(self)
        except Exception as exc:
            messagebox.showerror("Paper exposure", str(exc))
            self.paper_status_var.set(str(exc))
            return

        market_id = str(row["market_id"]).strip().lower()
        contract_id = str(row["contract_id"]).strip()
        key = (market_id, contract_id)
        current_rows = App._paper_position_rows(self.cfg.paper_trades)
        marks = App._paper_marks_for_rows(getattr(self, "_paper_position_marks", {}) or {}, current_rows)
        if key not in marks:
            self._paper_position_marks = marks
            App._refresh_paper_position_table(self)
            message = f"No paper exposure mark to clear for {market_id}:{contract_id}."
            self.paper_status_var.set(message)
            self.status_var.set("No selected paper exposure mark to clear.")
            return

        marks.pop(key, None)
        self._paper_position_marks = marks
        App._refresh_paper_position_table(self)

        message = f"Cleared selected paper exposure mark: {market_id}:{contract_id}"
        self.paper_status_var.set(message)
        self.status_var.set("Selected paper exposure mark cleared.")
        self.ui_queue.put(("log", f"[paper] {message}"))

    def clear_paper_position_marks(self) -> None:
        if not getattr(self, "_paper_position_marks", {}):
            self.paper_status_var.set("No paper exposure marks to clear.")
            self.status_var.set("No paper exposure marks to clear.")
            return

        self._paper_position_marks = {}
        App._refresh_paper_position_table(self)
        self.paper_status_var.set("Paper exposure marks cleared.")
        self.status_var.set("Paper exposure marks cleared.")
        self.ui_queue.put(("log", "[paper] Paper exposure marks cleared."))

    def _selected_paper_position_row(self) -> Dict[str, Any]:
        if not hasattr(self, "paper_position_tree"):
            raise ValueError("Paper exposure summary is not available.")
        selected = tuple(self.paper_position_tree.selection())
        if not selected:
            raise ValueError("Select a paper exposure row first.")
        values = tuple(self.paper_position_tree.item(selected[0], "values") or ())
        if len(values) < 3:
            raise ValueError("Selected paper exposure row is incomplete.")

        market_id = str(values[0] or "").strip().lower()
        contract_id = str(values[1] or "").strip()
        net_size = safe_float(values[2], None)
        if not market_id or not contract_id or net_size is None or net_size == 0:
            raise ValueError("Selected paper exposure row is incomplete.")
        return {"market_id": market_id, "contract_id": contract_id, "net_size": float(net_size)}

    def _refresh_paper_trade_table(self) -> None:
        if not hasattr(self, "paper_tree"):
            return
        for iid in self.paper_tree.get_children():
            self.paper_tree.delete(iid)
        for record in self.cfg.paper_trades:
            limit = "" if record.limit_price is None else f"{record.limit_price:g}"
            self.paper_tree.insert(
                "",
                "end",
                iid=record.id,
                values=(
                    time.strftime("%H:%M:%S", time.localtime(record.created_at)),
                    record.market_id,
                    record.contract_id,
                    record.side,
                    f"{record.size:g}",
                    limit,
                    "yes" if record.accepted else "no",
                    record.message,
                ),
            )
        App._refresh_paper_position_table(self)

    def _refresh_paper_position_table(self) -> None:
        if not hasattr(self, "paper_position_tree"):
            return
        for iid in self.paper_position_tree.get_children():
            self.paper_position_tree.delete(iid)
        rows = App._paper_position_rows(self.cfg.paper_trades)
        marks = App._paper_marks_for_rows(getattr(self, "_paper_position_marks", {}) or {}, rows)
        self._paper_position_marks = marks
        if hasattr(self, "paper_position_summary_var"):
            self.paper_position_summary_var.set(App._format_paper_position_summary(rows, marks))
        for row in rows:
            iid = f"{row['market_id']}:{row['contract_id']}"
            mark = marks.get((str(row["market_id"]), str(row["contract_id"])), {})
            mark_price = mark.get("mark_price")
            mark_source = str(mark.get("source") or "")
            mark_time = App._paper_mark_time_text(mark)
            unrealized = App._paper_position_mark_unrealized(row, mark)
            self.paper_position_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row["market_id"],
                    row["contract_id"],
                    f"{row['net_size']:.4f}",
                    "" if row["average_price"] is None else f"{row['average_price']:.4f}",
                    "" if row["notional"] is None else f"{row['notional']:.4f}",
                    "" if mark_price is None else f"{float(mark_price):.4f}",
                    mark_source,
                    mark_time,
                    "" if unrealized is None else f"{float(unrealized):.4f}",
                    row["trades"],
                ),
            )

    @staticmethod
    def _paper_position_rows(records: List[PaperTradeRecord]) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for record in records:
            if not record.accepted:
                continue
            signed_size = App._paper_record_signed_size(record)
            if signed_size == 0:
                continue
            price = record.average_price if record.average_price is not None else record.limit_price
            key = (record.market_id, record.contract_id)
            row = grouped.setdefault(
                key,
                {
                    "market_id": record.market_id,
                    "contract_id": record.contract_id,
                    "net_size": 0.0,
                    "notional": 0.0,
                    "priced_size": 0.0,
                    "trades": 0,
                },
            )
            row["net_size"] += signed_size
            row["trades"] += 1
            if price is not None:
                row["notional"] += signed_size * float(price)
                row["priced_size"] += abs(signed_size)

        rows: List[Dict[str, Any]] = []
        for row in grouped.values():
            priced_size = float(row.pop("priced_size"))
            notional = float(row["notional"])
            net_size = float(row["net_size"])
            row["average_price"] = abs(notional) / abs(net_size) if priced_size > 0 and net_size != 0 else None
            row["notional"] = notional if priced_size > 0 else None
            rows.append(row)
        return sorted(rows, key=lambda item: (str(item["market_id"]), str(item["contract_id"])))

    @staticmethod
    def _format_paper_position_summary(rows: List[Dict[str, Any]], marks: Dict[Tuple[str, str], Dict[str, Any]]) -> str:
        if not rows:
            return "No paper exposure."

        gross_size = sum(abs(float(row["net_size"])) for row in rows)
        priced_rows = [row for row in rows if row.get("notional") is not None]
        gross_entry = sum(abs(float(row["notional"])) for row in priced_rows)
        net_entry = sum(float(row["notional"]) for row in priced_rows)

        marked_count = 0
        last_marked_at: Optional[float] = None
        unrealized_values: List[float] = []
        mark_sources: Dict[str, int] = {}
        for row in rows:
            mark = marks.get((str(row["market_id"]), str(row["contract_id"])), {})
            mark_price = safe_float(mark.get("mark_price"), None)
            if mark_price is not None:
                marked_count += 1
            marked_at = safe_float(mark.get("marked_at"), None)
            if marked_at is not None:
                last_marked_at = marked_at if last_marked_at is None else max(last_marked_at, marked_at)
            unrealized = App._paper_position_mark_unrealized(row, mark)
            if unrealized is not None:
                unrealized_values.append(float(unrealized))
            source = str(mark.get("source") or "")
            if source:
                mark_sources[source] = mark_sources.get(source, 0) + 1

        parts = [
            f"Positions: {len(rows)}",
            f"gross_size={gross_size:.4f}",
            f"entry_notional={gross_entry:.4f}",
            f"net_notional={net_entry:.4f}",
            f"marked={marked_count}/{len(rows)}",
        ]
        if unrealized_values:
            parts.append(f"unrealized={sum(unrealized_values):.4f}")
        if last_marked_at is not None:
            parts.append(f"last_mark={time.strftime('%H:%M:%S', time.localtime(last_marked_at))}")
        if mark_sources:
            parts.append(
                "mark_sources=" + ",".join(f"{source}:{mark_sources[source]}" for source in sorted(mark_sources))
            )
        return "; ".join(parts)

    @staticmethod
    def _paper_marks_for_rows(
        marks: Dict[Tuple[str, str], Dict[str, Any]],
        rows: List[Dict[str, Any]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        active_keys = {(str(row["market_id"]), str(row["contract_id"])) for row in rows}
        pruned: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for key, value in marks.items():
            if not isinstance(key, tuple) or len(key) != 2:
                continue
            normalized_key = (str(key[0]), str(key[1]))
            if normalized_key in active_keys:
                pruned[normalized_key] = value
        return pruned

    @staticmethod
    def _paper_order_impact(records: List[PaperTradeRecord], order: PaperOrderRequest) -> Dict[str, Any]:
        current_row = next(
            (
                row
                for row in App._paper_position_rows(records)
                if row["market_id"] == order.market_id and row["contract_id"] == order.contract_id
            ),
            None,
        )
        current_net = float(current_row["net_size"]) if current_row else 0.0
        current_notional = current_row.get("notional") if current_row else None
        signed_size = App._paper_order_signed_size(order)
        projected_net = current_net + signed_size
        order_notional = signed_size * float(order.limit_price) if order.limit_price is not None else None
        projected_notional = (
            float(current_notional) + float(order_notional)
            if current_notional is not None and order_notional is not None
            else None
        )
        projected_average = (
            abs(projected_notional) / abs(projected_net)
            if projected_notional is not None and projected_net != 0
            else None
        )
        return {
            "market_id": order.market_id,
            "contract_id": order.contract_id,
            "side": order.side,
            "size": order.size,
            "limit_price": order.limit_price,
            "current_net": current_net,
            "signed_size": signed_size,
            "projected_net": projected_net,
            "effect": App._paper_order_impact_effect(current_net, signed_size, projected_net),
            "order_notional": order_notional,
            "projected_notional": projected_notional,
            "projected_average": projected_average,
        }

    @staticmethod
    def _format_paper_order_impact(impact: Dict[str, Any]) -> str:
        parts = [
            f"Impact: {impact['market_id']}:{impact['contract_id']}",
            f"{impact['side']} size={float(impact['size']):g}",
            f"current_net={float(impact['current_net']):.4f}",
            f"order_net={float(impact['signed_size']):.4f}",
            f"projected_net={float(impact['projected_net']):.4f}",
            f"effect={impact['effect']}",
        ]
        if impact.get("order_notional") is None:
            parts.append("limit blank")
        else:
            parts.append(f"order_notional={float(impact['order_notional']):.4f}")
        if impact.get("projected_notional") is not None:
            parts.append(f"projected_notional={float(impact['projected_notional']):.4f}")
        if impact.get("projected_average") is not None:
            parts.append(f"projected_avg={float(impact['projected_average']):.4f}")
        return "; ".join(parts)

    @staticmethod
    def _paper_order_signed_size(order: PaperOrderRequest) -> float:
        side = str(order.side or "").upper()
        size = float(order.size or 0.0)
        if side in {"SELL", "LAY"}:
            return -size
        if side in {"BUY", "BACK"}:
            return size
        return 0.0

    @staticmethod
    def _paper_order_impact_effect(current_net: float, signed_size: float, projected_net: float) -> str:
        if signed_size == 0:
            return "unchanged"
        if current_net == 0:
            return "opens position"
        if projected_net == 0:
            return "closes position"
        if (current_net > 0 > projected_net) or (current_net < 0 < projected_net):
            return "flips position"
        if (current_net > 0 and signed_size > 0) or (current_net < 0 and signed_size < 0):
            return "adds to position"
        return "reduces position"

    @staticmethod
    def _paper_record_signed_size(record: PaperTradeRecord) -> float:
        size = float(record.filled_size or record.size or 0.0)
        side = str(record.side or "").upper()
        if side in {"SELL", "LAY"}:
            return -size
        if side in {"BUY", "BACK"}:
            return size
        return 0.0

    @staticmethod
    def _paper_position_close_side(records: List[PaperTradeRecord], market_id: str, contract_id: str, net_size: float) -> str:
        matching_sides = [
            str(record.side or "").upper()
            for record in records
            if record.accepted and record.market_id == market_id and record.contract_id == contract_id
        ]
        uses_back_lay = any(side in {"BACK", "LAY"} for side in matching_sides)
        uses_buy_sell = any(side in {"BUY", "SELL"} for side in matching_sides)
        if uses_back_lay and not uses_buy_sell:
            return "LAY" if net_size > 0 else "BACK"
        return "SELL" if net_size > 0 else "BUY"

    @staticmethod
    def _paper_position_mark_price(snapshot: PriceSnapshot, net_size: float) -> Tuple[float, str]:
        values = AdapterPricePoller._snapshot_values(snapshot)
        ordered_sources = (
            (("best_bid", "bid"), ("midpoint", "midpoint"), ("last_trade", "last"), ("best_ask", "ask"))
            if net_size >= 0
            else (("best_ask", "ask"), ("midpoint", "midpoint"), ("last_trade", "last"), ("best_bid", "bid"))
        )
        for key, label in ordered_sources:
            value = values.get(key)
            if value is not None:
                return float(value), label
        raise ValueError("No mark price is available for this position.")

    @staticmethod
    def _paper_position_unrealized_pnl(row: Dict[str, Any], mark_price: float) -> Optional[float]:
        notional = row.get("notional")
        if notional is None:
            return None
        return float(row["net_size"]) * float(mark_price) - float(notional)

    @staticmethod
    def _paper_position_mark_unrealized(row: Dict[str, Any], mark: Dict[str, Any]) -> Optional[float]:
        mark_price = safe_float(mark.get("mark_price"), None)
        if mark_price is None:
            return None
        return App._paper_position_unrealized_pnl(row, mark_price)

    @staticmethod
    def _paper_mark_time_text(mark: Dict[str, Any]) -> str:
        marked_at = safe_float(mark.get("marked_at"), None)
        if marked_at is None:
            return ""
        return time.strftime("%H:%M:%S", time.localtime(marked_at))

    def clear_paper_history(self) -> None:
        if not self.cfg.paper_trades:
            return
        if not messagebox.askyesno("Clear paper history", "Clear local paper trade history?"):
            return
        self.cfg.paper_trades = []
        save_config(self.cfg)
        self._refresh_paper_trade_table()
        self.paper_status_var.set("Paper trade history cleared.")

    # ------------------ Market safety settings ------------------

    def _refresh_market_safety_tab(self) -> None:
        if not hasattr(self, "safety_market_var"):
            return
        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        market_cfg = App._market_config_for(self, market_id)
        settings = market_cfg.settings
        display_name = App._selected_market_display_name(self)

        self.safety_market_var.set(f"Selected market: {display_name} ({market_id})")
        self.safety_market_enabled_var.set(bool(market_cfg.enabled))
        self.safety_live_enabled_var.set(bool_from_setting(settings.get("live_trading_enabled"), False))
        self.safety_live_confirmed_var.set(
            bool_from_setting(settings.get("live_trading_confirmed"), False)
            or bool_from_setting(settings.get("live_trading_acknowledged"), False)
        )
        self.safety_kill_switch_var.set(
            bool_from_setting(settings.get("live_trading_kill_switch"), False)
            or bool_from_setting(settings.get("live_trading_paused"), False)
        )
        self.safety_max_size_var.set("" if settings.get("live_trading_max_size") in (None, "") else str(settings.get("live_trading_max_size")))
        self.safety_max_notional_var.set(
            "" if settings.get("live_trading_max_notional") in (None, "") else str(settings.get("live_trading_max_notional"))
        )

        try:
            adapter = App._get_selected_market_adapter(self)
            health = adapter.health_check()
            status = App._selected_market_status_text(self, adapter)
        except Exception as exc:
            health = {"ok": False, "message": str(exc)}
            status = f"{display_name}: adapter unavailable. {exc}"

        self.safety_status_var.set(status)
        App._refresh_market_health_table(self, health)

    def _refresh_market_health_table(self, health: Dict[str, Any]) -> None:
        if not hasattr(self, "safety_tree"):
            return
        for iid in self.safety_tree.get_children():
            self.safety_tree.delete(iid)

        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        market_cfg = App._market_config_for(self, market_id)
        settings = market_cfg.settings
        capabilities = health.get("capabilities")
        enabled_capabilities = []
        if isinstance(capabilities, dict):
            enabled_capabilities = [key for key, value in capabilities.items() if value]

        rows = [
            ("market_id", market_id),
            ("enabled", "yes" if market_cfg.enabled else "no"),
            ("adapter", str(health.get("adapter") or "")),
            ("health", "ok" if health.get("ok") else "not ok"),
            ("message", str(health.get("message") or "")),
            ("capabilities", ", ".join(enabled_capabilities) if enabled_capabilities else "none"),
            ("credential_env_vars", ", ".join(str(v) for v in settings.get("credential_env_vars") or []) or "none listed"),
            ("credential_sources", App._format_credential_sources(health.get("credential_sources"))),
            ("live_trading_enabled", str(bool_from_setting(settings.get("live_trading_enabled"), False)).lower()),
            ("live_trading_confirmed", str(self.safety_live_confirmed_var.get()).lower()),
            ("live_trading_kill_switch", str(self.safety_kill_switch_var.get()).lower()),
            ("live_trading_max_size", str(settings.get("live_trading_max_size") or "")),
            ("live_trading_max_notional", str(settings.get("live_trading_max_notional") or "")),
        ]

        for key, value in rows:
            self.safety_tree.insert("", "end", iid=key, values=(key, value))

    @staticmethod
    def _format_credential_sources(raw: Any) -> str:
        if not isinstance(raw, list) or not raw:
            return "none detected"
        parts = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "credential")
                source = str(item.get("source") or "configured")
                parts.append(f"{name} from {source}")
        return ", ".join(parts) if parts else "none detected"

    def save_market_safety_settings(self) -> None:
        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        market_cfg = App._market_config_for(self, market_id)
        settings = dict(market_cfg.settings)

        try:
            max_size = optional_positive_float(self.safety_max_size_var.get(), "Max order size")
            max_notional = optional_positive_float(self.safety_max_notional_var.get(), "Max notional")
        except Exception as exc:
            messagebox.showerror("Market safety settings", str(exc))
            self.safety_status_var.set(str(exc))
            return

        market_cfg.enabled = bool(self.safety_market_enabled_var.get())
        settings["live_trading_enabled"] = bool(self.safety_live_enabled_var.get())
        settings["live_trading_confirmed"] = bool(self.safety_live_confirmed_var.get())
        settings["live_trading_kill_switch"] = bool(self.safety_kill_switch_var.get())
        if max_size is None:
            settings.pop("live_trading_max_size", None)
        else:
            settings["live_trading_max_size"] = max_size
        if max_notional is None:
            settings.pop("live_trading_max_notional", None)
        else:
            settings["live_trading_max_notional"] = max_notional
        market_cfg.settings = settings
        self.cfg.markets[market_id] = market_cfg

        save_config(self.cfg)
        if hasattr(self, "market_status_var"):
            self.market_status_var.set(App._selected_market_status_text(self))
        if hasattr(self, "paper_market_var"):
            self.paper_market_var.set(market_id)
            App._refresh_paper_market_state(self)
        App._refresh_market_safety_tab(self)
        self.status_var.set("Market safety settings saved.")
        self.ui_queue.put(("log", f"[market] Saved safety settings for {market_id}."))

    # ------------------ Wallet tracking ------------------

    def _wallet_choices(self) -> List[str]:
        return [w.wallet for w in self.cfg.wallets]

    def _refresh_wallet_table(self):
        for iid in self.wallet_tree.get_children():
            self.wallet_tree.delete(iid)
        for w in self.cfg.wallets:
            last_seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(w.last_seen_ts)) if w.last_seen_ts else ""
            name = w.display_name or w.wallet[:10] + "..."
            self.wallet_tree.insert(
                "",
                "end",
                iid=w.id,
                values=(name, w.wallet, "yes" if w.enabled else "no", last_seen),
            )
        # refresh copy tab dropdown too
        try:
            self.ct_follow_combo["values"] = self._wallet_choices()
        except Exception:
            pass

    def _selected_wallet_id(self) -> Optional[str]:
        sel = self.wallet_tree.selection()
        return sel[0] if sel else None

    def toggle_selected_wallet(self):
        wid = self._selected_wallet_id()
        if not wid:
            return
        for w in self.cfg.wallets:
            if w.id == wid:
                w.enabled = not w.enabled
                save_config(self.cfg)
                self._refresh_wallet_table()
                self.ui_queue.put(("log", f"Toggled wallet {w.wallet} -> {'enabled' if w.enabled else 'disabled'}"))
                return

    def delete_selected_wallet(self):
        wid = self._selected_wallet_id()
        if not wid:
            return
        self.cfg.wallets = [w for w in self.cfg.wallets if w.id != wid]
        save_config(self.cfg)
        self._refresh_wallet_table()
        self.ui_queue.put(("log", f"Deleted wallet watch {wid}"))

    def search_or_add_wallet(self):
        q = (self.wallet_search_entry.get() or "").strip()
        if not q:
            return

        # Non-Polymarket activity feeds use their documented identity format.
        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        if market_id != "polymarket":
            if not self._require_selected_market_capability("Wallet tracking", "copy_trading"):
                return
            wallet = normalize_activity_identity(market_id, q)
            if not wallet:
                expected = "manifold:<username>" if market_id == "manifold" else "a 0x wallet address"
                messagebox.showerror(
                    "Wallet tracking",
                    f"The selected market requires {expected}; username search is unavailable.",
                )
                return
            display_name = wallet.split(":", 1)[1] if market_id == "manifold" else wallet[:10] + "..."
            self._add_wallet_watch(wallet, display_name=display_name)
            return

        if not self._require_polymarket_selected("Wallet tracking"):
            return
        w = normalize_wallet(q)
        if w:
            self._add_wallet_watch(w, display_name=w[:10] + "...")
            return

        # treat as username/pseudonym
        self.status_var.set("Searching profiles...")
        self.update_idletasks()
        try:
            profiles = self._get_polymarket_adapter().search_profiles(q, limit=10)
            if not profiles:
                messagebox.showinfo("No results", "No profiles found. Try pasting the 0x wallet address instead.")
                self.status_var.set("No profiles found.")
                return

            # popup select
            win = tk.Toplevel(self)
            win.title("Select profile")
            win.geometry("700x300")
            self._apply_window_icon(win)
            lb = tk.Listbox(win)
            palette = self._palette
            win.configure(background=palette["bg"])
            lb.configure(
                bg=palette["field_bg"],
                fg=palette["field_fg"],
                selectbackground=palette["select_bg"],
                selectforeground=palette["select_fg"],
                highlightbackground=palette["border"],
                highlightcolor=palette["border"],
            )
            lb.pack(fill="both", expand=True, padx=10, pady=10)

            for p in profiles:
                lb.insert("end", f"{p.pseudonym}  |  {p.proxy_wallet}")

            def add_selected():
                sel = lb.curselection()
                if not sel:
                    return
                p = profiles[sel[0]]
                self._add_wallet_watch(p.proxy_wallet, display_name=p.pseudonym or p.proxy_wallet[:10]+"...")
                win.destroy()

            ttk.Button(win, text="Add", command=add_selected).pack(pady=10)
            self.status_var.set("Select a profile to track.")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.status_var.set("Profile search error.")

    def _add_wallet_watch(self, wallet: str, display_name: str = ""):
        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        normalized = normalize_activity_identity(market_id, wallet)
        if not normalized:
            expected = "manifold:<username>" if market_id == "manifold" else "0x wallet/proxyWallet address"
            messagebox.showerror("Error", f"Not a valid {expected}.")
            return
        wallet = normalized
        if any(w.wallet == wallet for w in self.cfg.wallets):
            messagebox.showinfo("Already tracked", "This wallet is already being tracked.")
            return

        ww = WalletWatch(wallet=wallet, display_name=display_name, enabled=True)
        self.cfg.wallets.append(ww)
        save_config(self.cfg)
        self._refresh_wallet_table()
        self.ui_queue.put(("log", f"Added wallet watch: {display_name} ({wallet})"))

    def toggle_wallet_polling(self):
        if self.wallet_poller._thread.is_alive():
            self.wallet_poller.stop()
            self.status_var.set("Wallet polling stopped.")
            self.ui_queue.put(("log", "Wallet polling stopped."))
            return
        if not self._require_selected_market_capability("Wallet polling", "copy_trading"):
            return

        iv = safe_float(self.poll_interval_var.get(), default=10.0)
        if iv is None or iv < 2:
            iv = 10.0
        self.wallet_poller.poll_interval = float(iv)
        self.wallet_poller.start()
        self.status_var.set(f"Wallet polling started (every {iv:.0f}s).")
        self.ui_queue.put(("log", f"Wallet polling started (interval={iv:.0f}s)."))

    # ------------------ Copy trading ------------------

    def do_geoblock_check(self):
        if not self._require_polymarket_selected("Geoblock checks"):
            return
        self.status_var.set("Checking geoblock...")
        self.update_idletasks()
        try:
            geo = self._get_polymarket_adapter().check_geoblock()
            self._geoblock_cache = geo
            if geo.get("blocked") is True:
                self.geo_var.set(f"BLOCKED ({geo.get('country')} {geo.get('region')})")
            else:
                self.geo_var.set(f"OK ({geo.get('country')} {geo.get('region')})")
            self.ui_queue.put(("log", f"Geoblock: {geo}"))
            self.status_var.set("Geoblock check done.")
        except Exception as e:
            self.geo_var.set(f"Error: {e}")
            self.ui_queue.put(("log", f"Geoblock error: {e}"))
            self.status_var.set("Geoblock check error.")

    def _copy_follow_wallets_from_text(self) -> Optional[List[str]]:
        raw = str(self.ct_follow_var.get() or "")
        market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        wallets: List[str] = []
        for part in re.split(r"[,;\s]+", raw):
            candidate = part.strip()
            if not candidate:
                continue
            wallet = normalize_activity_identity(market_id, candidate)
            if not wallet:
                hint = activity_identity_hint(market_id)
                messagebox.showerror("Copy trading settings", f"Invalid follow identity: {candidate} (use {hint}).")
                return None
            if wallet not in wallets:
                wallets.append(wallet)
        return wallets

    def save_copy_settings(self):
        if not self._require_selected_market_capability("Copy trading settings", "copy_trading"):
            return
        follow_wallets = self._copy_follow_wallets_from_text()
        if follow_wallets is None:
            return
        copy_percentage = safe_float(self.ct_scale_var.get(), None)
        max_usdc = safe_float(self.ct_max_var.get(), None)
        slippage = safe_float(self.ct_slip_var.get(), None)
        if copy_percentage is None or copy_percentage < 0 or copy_percentage > 100:
            messagebox.showerror("Copy trading settings", "Copy percentage must be a number between 0 and 100.")
            return
        if max_usdc is None or max_usdc <= 0:
            messagebox.showerror("Copy trading settings", "Max USDC / trade must be a positive number.")
            return
        if slippage is None or slippage < 0 or slippage > 1:
            messagebox.showerror("Copy trading settings", "Slippage must be a number between 0 and 1.")
            return
        s = CopyTradeSettings(
            enabled=bool(self.ct_enabled_var.get()),
            live=bool(self.ct_live_var.get()),
            follow_wallet=follow_wallets[0] if follow_wallets else "",
            follow_wallets=follow_wallets,
            scale=float(copy_percentage) / 100.0,
            max_usdc_per_trade=float(max_usdc),
            slippage=float(slippage),
            allow_sells=bool(self.ct_allow_sells_var.get()),
            conflict_guard=bool(self.ct_conflict_guard_var.get()),
            conflict_window_seconds=self.cfg.copytrading.conflict_window_seconds,
        )
        self.ct_follow_var.set(", ".join(s.normalized_follow_wallets()))
        self.cfg.copytrading = s
        save_config(self.cfg)
        self.ui_queue.put(("log", f"Saved copy settings: {asdict(s)}"))

        # Safety: warn on LIVE
        if s.live and s.enabled:
            messagebox.showwarning(
                "LIVE Copy Trading Enabled",
                "LIVE mode will place real orders.\n\n"
                "Make sure:\n"
                "- You are not geoblocked\n"
                "- Your PRIVATE_KEY / FUNDER_ADDRESS / SIGNATURE_TYPE are correct\n"
                "- You understand the risk controls\n",
            )

    def _get_trader(self) -> PolymarketTrader:
        if self._trader is not None:
            return self._trader

        pk = (os.getenv("PRIVATE_KEY") or "").strip()
        if not pk:
            raise RuntimeError("Missing PRIVATE_KEY in environment/.env")
        funder = (os.getenv("FUNDER_ADDRESS") or "").strip() or None
        sig_type = int((os.getenv("SIGNATURE_TYPE") or "0").strip())

        cfg = TraderConfig(private_key=pk, funder_address=funder, signature_type=sig_type)
        self._trader = PolymarketTrader(cfg)
        return self._trader

    @staticmethod
    def _copy_guard_key(item: Dict[str, Any]) -> str:
        parts = (
            str(item.get("asset") or "").strip().lower(),
            str(item.get("slug") or "").strip().lower(),
            str(item.get("outcome") or "").strip().lower(),
        )
        return "|".join(part for part in parts if part)

    def _copy_conflict_reason(self, item: Dict[str, Any]) -> Optional[str]:
        settings = self.cfg.copytrading
        if not settings.conflict_guard:
            return None
        key = App._copy_guard_key(item)
        if not key:
            return None
        side = str(item.get("side") or "").upper()
        wallet = str(item.get("proxyWallet") or "").strip().lower()
        timestamp = int(safe_float(item.get("timestamp"), time.time()) or time.time())
        window = max(0, int(settings.conflict_window_seconds or 0))
        if window:
            stale = [
                state_key
                for state_key, state in self._copy_conflict_cache.items()
                if timestamp - int(state.get("timestamp") or timestamp) > window
            ]
            for state_key in stale:
                self._copy_conflict_cache.pop(state_key, None)
        existing = self._copy_conflict_cache.get(key)
        if existing:
            previous_side = str(existing.get("side") or "").upper()
            previous_wallet = str(existing.get("wallet") or "")
            if previous_wallet == wallet:
                return None
            if previous_side and previous_side != side:
                return f"opposite-side same-token copy already accepted from {previous_wallet}"
            return f"duplicate same-token copy already accepted from {previous_wallet}"
        return None

    def _commit_copy_conflict(self, item: Dict[str, Any]) -> None:
        settings = self.cfg.copytrading
        if not settings.conflict_guard:
            return
        key = App._copy_guard_key(item)
        if not key:
            return
        self._copy_conflict_cache[key] = {
            "side": str(item.get("side") or "").upper(),
            "wallet": str(item.get("proxyWallet") or "").strip().lower(),
            "timestamp": int(safe_float(item.get("timestamp"), time.time()) or time.time()),
            "activity_key": activity_key(item),
        }

    def _copy_trade_from_activity(
        self,
        item: Dict[str, Any],
        *,
        market_id: str = "",
        execution_policy: Optional[Dict[str, Any]] = None,
        staged_at: int = 0,
        replay_authorized_at: int = 0,
        before_live_dispatch: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> CopyActivityOutcome:
        now = time.time()
        manual_replay_authorized = int(replay_authorized_at or 0) > 0
        replay_age_origin = int(replay_authorized_at or staged_at or 0)
        replay_age = now - replay_age_origin if replay_age_origin else 0.0
        if replay_age_origin and (
            replay_age > _COPY_ACTIVITY_MAX_REPLAY_AGE_SECONDS or replay_age < -60
        ):
            return CopyActivityOutcome(
                "rejected",
                "copy_activity_stale",
                "The copy signal exceeded the automatic replay age limit.",
            )
        selected_market_id = str(self.cfg.selected_market_id or "polymarket").strip().lower()
        market_id = str(market_id or selected_market_id).strip().lower()
        if market_id != selected_market_id:
            self.ui_queue.put(
                (
                    "log",
                    "[copy] Source market changed before activity handling; refusing cross-market replay.",
                )
            )
            return CopyActivityOutcome(
                "retryable",
                "source_market_mismatch",
                "Select the activity's durable source market before retrying it.",
            )
        current_policy = _copy_execution_policy(self.cfg.copytrading)
        if execution_policy is not None:
            if not execution_policy:
                return CopyActivityOutcome(
                    "ambiguous",
                    "execution_policy_missing",
                    "Legacy replay state has no bound execution policy; manual reconciliation is required.",
                )
            if dict(execution_policy) != current_policy:
                return CopyActivityOutcome(
                    "retryable",
                    "execution_policy_changed",
                    "Copy settings changed after staging; restore the original policy or reconcile manually.",
                )
            s = CopyTradeSettings.from_dict(execution_policy)
        else:
            s = self.cfg.copytrading
        if s.live:
            raw_activity_timestamp = safe_float(item.get("timestamp"), None)
            if raw_activity_timestamp is None or raw_activity_timestamp <= 0:
                return CopyActivityOutcome(
                    "rejected",
                    "live_activity_timestamp_missing",
                    "Live copy dispatch requires a source activity timestamp.",
                )
            activity_timestamp = float(raw_activity_timestamp)
            if activity_timestamp >= 1_000_000_000_000:
                activity_timestamp /= 1000.0
            activity_age = now - activity_timestamp
            if (
                activity_age < -60
                or (
                    activity_age > _COPY_ACTIVITY_MAX_REPLAY_AGE_SECONDS
                    and not manual_replay_authorized
                )
            ):
                return CopyActivityOutcome(
                    "rejected",
                    "live_activity_timestamp_stale",
                    "The source activity timestamp is outside the live-copy freshness window.",
                )
        if not s.enabled:
            return CopyActivityOutcome("rejected", "copy_disabled", "Copy trading is disabled.")
        followed_wallets = set(s.normalized_follow_wallets())
        if not followed_wallets:
            return CopyActivityOutcome("rejected", "no_follow_wallets", "No copy-trading wallets are configured.")

        activity_identity = normalize_activity_identity(market_id, item.get("proxyWallet"))
        if not activity_identity or activity_identity not in followed_wallets:
            return CopyActivityOutcome("rejected", "wallet_not_followed", "Activity wallet is not followed.")

        side = str(item.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            return CopyActivityOutcome("rejected", "invalid_side", "Activity side is not copyable.")
        if side == "SELL" and not s.allow_sells:
            self.ui_queue.put(("log", "[copy] Skipping SELL trade (allow_sells=false)."))
            return CopyActivityOutcome("rejected", "sells_disabled", "SELL copy trading is disabled.")

        token_id = str(item.get("asset") or item.get("contract_id") or "").strip()
        if not token_id:
            return CopyActivityOutcome("rejected", "missing_contract", "Activity has no contract identifier.")

        raw_size = safe_float(item.get("size"), 0.0) or 0.0
        raw_price = safe_float(item.get("price"), None)
        size = max(0.0, raw_size * float(s.scale))
        adapter = (
            self._get_polymarket_adapter()
            if market_id == "polymarket"
            else App._adapter_for_market(self, market_id)
        )
        capabilities = getattr(adapter, "capabilities", None)
        if capabilities is not None and not bool(getattr(capabilities, "copy_trading", False)):
            self.ui_queue.put(("log", f"[copy] {adapter.display_name} does not support copy trading."))
            return CopyActivityOutcome(
                "rejected",
                "copy_not_supported",
                "The selected market does not support copy trading.",
            )

        # Pull current best bid/ask for safer limit pricing
        best_bid = best_ask = None
        is_azuro = market_id == "azuro"
        if not is_azuro:
            try:
                book = adapter.get_orderbook(token_id)
                best_bid = book.bids[0].price if book.bids else None
                best_ask = book.asks[0].price if book.asks else None
            except Exception:
                pass

        if s.live and market_id == "polymarket":
            executable_quote = best_ask if side == "BUY" else best_bid
            if executable_quote is None:
                self.ui_queue.put(
                    ("log", "[copy LIVE] fresh executable-side orderbook quote is unavailable.")
                )
                return CopyActivityOutcome(
                    "retryable",
                    "live_quote_unavailable",
                    "A fresh executable-side quote is required before live dispatch.",
                )

        slip = max(0.0, min(float(s.slippage), 1.0))
        if is_azuro:
            raw_odds = safe_float(item.get("odds"), None)
            if raw_odds is None and raw_price is not None and raw_price > 0:
                raw_odds = 1.0 / raw_price
            if raw_odds is None or raw_odds <= 0:
                self.ui_queue.put(("log", "[copy] Azuro activity has no valid decimal odds."))
                return CopyActivityOutcome(
                    "rejected",
                    "invalid_odds",
                    "Activity has no valid decimal odds.",
                )
            # Azuro's minimum-odds field is a decimal-odds floor. Allow the
            # configured slippage to reduce that floor, rather than treating
            # it as a 0..1 probability cap.
            limit_price = max(1e-12, float(raw_odds) * (1.0 - slip))
        elif side == "BUY":
            # pick an ask-based price cap
            ref = best_ask if best_ask is not None else raw_price
            if ref is None:
                ref = 0.99
            limit_price = min(1.0, float(ref) + slip)
        else:
            ref = best_bid if best_bid is not None else raw_price
            if ref is None:
                ref = 0.01
            limit_price = max(0.0, float(ref) - slip)

        # Enforce max USDC exposure by shrinking share size
        max_usdc = max(0.01, float(s.max_usdc_per_trade))
        buy_budget_activity = market_id in {"azuro", "manifold", "myriad_markets"} and side == "BUY"
        if buy_budget_activity:
            if size > max_usdc:
                size = max_usdc
        elif limit_price > 0:
            max_shares = max_usdc / limit_price
            if size > max_shares:
                size = max_shares

        if size <= 0:
            return CopyActivityOutcome("rejected", "zero_size", "Scaled copy order size is zero.")
        conflict_reason = App._copy_conflict_reason(self, item)
        if conflict_reason:
            self.ui_queue.put(("log", f"[copy] Conflict guard skipped {token_id[:10]}...: {conflict_reason}."))
            return CopyActivityOutcome(
                "rejected",
                "conflict_guard",
                "Copy conflict guard rejected this activity.",
            )

        if not s.live:
            order_metadata: Dict[str, Any] = {"source": "copy_trading", "activity": dict(item)}
            if market_id in {"manifold", "myriad_markets"} and side == "SELL":
                order_metadata["shares"] = item.get("shares") or size
            order = PaperOrderRequest(
                market_id=market_id,
                contract_id=token_id,
                side=side,
                size=size,
                limit_price=limit_price,
                metadata=order_metadata,
            )
            try:
                paper_result = adapter.place_paper_order(order)
            except (AttributeError, UnsupportedFeatureError):
                # Keep compatibility with lightweight adapters used by the
                # desktop smoke tests; the guarded simulation log is still
                # useful when no paper ledger is available.
                paper_result = None
            except Exception as exc:
                self.ui_queue.put(
                    ("log", f"[copy SIM] {market_id} paper order failed before live dispatch ({type(exc).__name__}).")
                )
                return CopyActivityOutcome(
                    "retryable",
                    "paper_order_failure",
                    "Paper-order handling failed and can be retried safely.",
                )
            suffix = ""
            if paper_result is not None:
                suffix = f" accepted={bool(paper_result.accepted)}"
            market_prefix = "" if market_id == "polymarket" else f"{market_id} "
            self.ui_queue.put(
                (
                    "log",
                    f"[copy SIM] {market_prefix}{side} token={token_id[:10]}... "
                    f"size={size:.4f} price<= {limit_price:.4f}{suffix}",
                )
            )
            if paper_result is not None and not bool(paper_result.accepted):
                return CopyActivityOutcome(
                    "rejected",
                    "paper_order_rejected",
                    "The paper-order adapter rejected the simulated order.",
                )
            App._commit_copy_conflict(self, item)
            return CopyActivityOutcome(
                "completed",
                "paper_order_completed",
                "Copy simulation completed without live dispatch.",
            )

        if market_id != "polymarket":
            self.ui_queue.put(
                (
                    "log",
                    f"[copy LIVE] {adapter.display_name} live copy trading is disabled; "
                    "use simulation until its official signing SDK is integrated.",
                )
            )
            return CopyActivityOutcome(
                "rejected",
                "live_market_not_supported",
                "Live copy trading is not supported for the selected market.",
            )

        # LIVE mode safety: never rely on a cached or UI-initiated result. A
        # fresh, explicit ``blocked is False`` response is required for every
        # dispatch attempt; errors and malformed responses fail closed before
        # the non-replayable dispatch boundary.
        try:
            geoblock = adapter.check_geoblock()
        except Exception as exc:
            self.ui_queue.put(("log", f"[copy LIVE] fresh geoblock check failed ({type(exc).__name__})."))
            return CopyActivityOutcome(
                "retryable",
                "geoblock_check_failure",
                "Fresh geoblock verification failed before dispatch and may be retried.",
            )
        if not isinstance(geoblock, dict) or not isinstance(geoblock.get("blocked"), bool):
            self.ui_queue.put(("log", "[copy LIVE] geoblock status is unknown; refusing order."))
            return CopyActivityOutcome(
                "retryable",
                "geoblock_status_unknown",
                "Fresh geoblock verification returned no conclusive status.",
            )
        self._geoblock_cache = {**geoblock, "checked_at": int(time.time())}
        if geoblock.get("blocked") is True:
            self.ui_queue.put(("log", "[copy] BLOCKED by geoblock. Refusing to place order."))
            return CopyActivityOutcome(
                "rejected",
                "geoblock",
                "Live copy order was rejected by the geoblock safety gate.",
            )

        order = PaperOrderRequest(
            market_id=market_id,
            contract_id=token_id,
            side=side,
            size=size,
            limit_price=limit_price,
            metadata={"source": "copy_trading", "tif": "FOK"},
        )
        try:
            preflight = adapter.preflight_live_order(order, feature_name="live copy trading")
        except Exception as e:
            self.ui_queue.put(("log", f"[copy LIVE] preflight blocked order ({type(e).__name__})."))
            if isinstance(e, (MarketConfigurationError, UnsupportedFeatureError, ValueError)):
                return CopyActivityOutcome(
                    "rejected",
                    "live_preflight_rejected",
                    "Live-order preflight conclusively rejected this copy order.",
                )
            return CopyActivityOutcome(
                "retryable",
                "live_preflight_failure",
                "Live-order preflight failed before dispatch and may be retried.",
            )

        try:
            trader = self._get_trader()
        except Exception as exc:
            self.ui_queue.put(("log", f"[copy LIVE] trader setup failed ({type(exc).__name__})."))
            return CopyActivityOutcome(
                "retryable",
                "trader_setup_failure",
                "Trader setup failed before dispatch and may be retried.",
            )
        dispatch = {
            "market_id": market_id,
            "contract_id": token_id,
            "side": side,
            "size": size,
            "limit_price": limit_price,
            "tif": "FOK",
            "approx_notional": float(preflight["approx_notional"]),
        }
        if before_live_dispatch is None:
            self.ui_queue.put(("log", "[copy LIVE] durable dispatch outbox is unavailable; refusing order."))
            return CopyActivityOutcome(
                "retryable",
                "dispatch_persistence_unavailable",
                "Live dispatch was refused because its durable outbox was unavailable.",
            )
        try:
            before_live_dispatch(dispatch)
        except Exception as exc:
            self.ui_queue.put(("log", f"[copy LIVE] dispatch intent persistence failed ({type(exc).__name__})."))
            return CopyActivityOutcome(
                "retryable",
                "dispatch_intent_persistence_failure",
                "Dispatch intent could not be persisted; no live order was sent.",
            )
        App._commit_copy_conflict(self, item)
        self.ui_queue.put(
            (
                "log",
                "[copy LIVE] Placing "
                f"{side} order token={token_id} size={size:.4f} price={limit_price:.4f} "
                f"notional~{preflight['approx_notional']:.4f} FOK",
            )
        )
        try:
            resp = trader.place_limit_order(token_id=token_id, side=side, price=limit_price, size=size, tif="FOK")
            self.ui_queue.put(("log", f"[copy LIVE] response: {resp}"))
        except Exception as e:
            self.ui_queue.put(("log", f"[copy LIVE] dispatch outcome is ambiguous ({type(e).__name__})."))
            return CopyActivityOutcome(
                "ambiguous",
                "live_dispatch_ambiguous",
                "The venue call did not return a conclusive result; manual reconciliation is required.",
                dispatch,
            )
        return CopyActivityOutcome(
            "completed",
            "live_dispatch_completed",
            "The venue returned a conclusive live-order response.",
            dispatch,
        )

    # ------------------ Market WS event handling ------------------

    def _on_market_event_bg(self, data: Dict[str, Any]):
        # Called from WS thread; push to UI thread
        self.ui_queue.put(("market_event", None, data))

    def _update_price_state(self, ev: Dict[str, Any]):
        et = ev.get("event_type")
        if not et:
            return

        if et == "last_trade_price":
            token_id = str(ev.get("asset_id") or "")
            price = safe_float(ev.get("price"))
            if not token_id or price is None:
                return
            st = self.price_state.setdefault(self._price_state_key("polymarket", token_id), {})
            st["last_trade"] = price
            # Evaluate alerts
            self._eval_alerts_for_contract("polymarket", token_id)

        elif et == "best_bid_ask":
            token_id = str(ev.get("asset_id") or "")
            bid = safe_float(ev.get("best_bid"))
            ask = safe_float(ev.get("best_ask"))
            if not token_id:
                return
            st = self.price_state.setdefault(self._price_state_key("polymarket", token_id), {})
            if bid is not None:
                st["best_bid"] = bid
            if ask is not None:
                st["best_ask"] = ask
            if st.get("best_bid") is not None and st.get("best_ask") is not None:
                st["midpoint"] = (st["best_bid"] + st["best_ask"]) / 2.0  # type: ignore
            self._eval_alerts_for_contract("polymarket", token_id)

        elif et == "price_change":
            changes = ev.get("price_changes") or []
            if not isinstance(changes, list):
                return
            for ch in changes:
                token_id = str(ch.get("asset_id") or "")
                if not token_id:
                    continue
                bid = safe_float(ch.get("best_bid"))
                ask = safe_float(ch.get("best_ask"))
                st = self.price_state.setdefault(self._price_state_key("polymarket", token_id), {})
                if bid is not None:
                    st["best_bid"] = bid
                if ask is not None:
                    st["best_ask"] = ask
                if st.get("best_bid") is not None and st.get("best_ask") is not None:
                    st["midpoint"] = (st["best_bid"] + st["best_ask"]) / 2.0  # type: ignore
                self._eval_alerts_for_contract("polymarket", token_id)

        elif et == "book":
            token_id = str(ev.get("asset_id") or "")
            if not token_id:
                return
            bid = ask = None
            try:
                bids = ev.get("bids") or ev.get("buys") or []
                asks = ev.get("asks") or ev.get("sells") or []
                if bids:
                    bid = safe_float(bids[0].get("price"))
                if asks:
                    ask = safe_float(asks[0].get("price"))
            except Exception:
                pass
            st = self.price_state.setdefault(self._price_state_key("polymarket", token_id), {})
            if bid is not None:
                st["best_bid"] = bid
            if ask is not None:
                st["best_ask"] = ask
            if st.get("best_bid") is not None and st.get("best_ask") is not None:
                st["midpoint"] = (st["best_bid"] + st["best_ask"]) / 2.0  # type: ignore
            self._eval_alerts_for_contract("polymarket", token_id)

    def _eval_alerts_for_token(self, token_id: str):
        App._eval_alerts_for_contract(self, "polymarket", token_id)

    def _update_adapter_price_state(self, payload: Dict[str, Any]) -> None:
        market_id = str(payload.get("market_id") or "").strip().lower()
        contract_id = str(payload.get("contract_id") or "")
        values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
        if not market_id or not contract_id:
            return
        st = self.price_state.setdefault(App._price_state_key(market_id, contract_id), {})
        for key in ("last_trade", "midpoint", "best_bid", "best_ask"):
            value = safe_float(values.get(key), None)
            if value is not None:
                st[key] = value
        self._eval_alerts_for_contract(market_id, contract_id)

    def _eval_alerts_for_contract(self, market_id: str, contract_id: str):
        normalized_market = str(market_id or "polymarket").strip().lower()
        st = self.price_state.get(App._price_state_key(normalized_market, contract_id)) or {}
        # evaluate all alerts for this token
        changed = False
        for a in self.cfg.alerts:
            if not a.enabled:
                continue
            if App._alert_market_id(a) != normalized_market:
                continue
            if a.token_id != contract_id:
                continue

            val = st.get(a.source)
            if val is None:
                continue

            prev = a.last_value
            a.last_value = float(val)

            cond_now = (val >= a.threshold) if a.direction == "above" else (val <= a.threshold)
            cond_prev = None
            if prev is not None:
                cond_prev = (prev >= a.threshold) if a.direction == "above" else (prev <= a.threshold)

            # Trigger only on crossing into the condition
            crossed = cond_now and (cond_prev is False or cond_prev is None)

            if crossed and not a.triggered:
                a.triggered = True
                changed = True
                self._fire_alert(a, val)

                if a.once:
                    a.enabled = False

            # For repeat alerts: reset triggered flag when condition becomes false again
            if not a.once and a.triggered and not cond_now:
                a.triggered = False
                changed = True

        if changed:
            save_config(self.cfg)
            self._refresh_alert_table()

    def _fire_alert(self, alert: PriceAlert, value: float):
        msg = (
            f"ALERT: {self._alert_market_id(alert)}:{alert.label} | "
            f"{alert.source}={value:.3f} crossed {alert.direction} {alert.threshold:.3f}"
        )
        self.ui_queue.put(("log", msg))
        try:
            self.bell()
        except Exception:
            pass
        # Non-blocking popup
        self.after(0, lambda: messagebox.showinfo("Price Alert", msg))

    # ------------------ Queue processing ------------------

    def _process_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2:
                    kind, a = item
                    b = None
                elif isinstance(item, tuple) and len(item) == 3:
                    kind, a, b = item
                else:
                    kind, a, b = "log", f"[debug] unknown queue item: {item}", None
                if kind == "log":
                    self.log(str(a))
                elif kind == "market_event":
                    self._update_price_state(b)  # type: ignore
                elif kind == "adapter_price":
                    self._update_adapter_price_state(a)  # type: ignore
                elif kind == "wallet_activity":
                    task = (
                        a
                        if isinstance(a, WalletActivityTask)
                        else WalletActivityTask(str(a), dict(b or {}), activity_key(b or {}))
                    )
                    checkpoint = None
                    try:
                        checkpoint = _apply_wallet_activity_checkpoint(self.cfg, task)
                        if checkpoint is None:
                            task.acknowledge(True)
                            continue
                        if checkpoint.changed:
                            save_config(self.cfg)
                    except Exception as exc:
                        if checkpoint is not None:
                            _restore_wallet_activity_checkpoint(checkpoint)
                        self.log(
                            "[copy] Refusing wallet activity because its replay checkpoint "
                            f"could not be persisted ({type(exc).__name__})."
                        )
                        task.acknowledge(False)
                        continue

                    outbox_entry = _find_copy_activity_entry(
                        self.cfg,
                        task.watch_id,
                        task.checkpoint_key,
                        task.market_id,
                    )
                    if outbox_entry is None:
                        self.log(
                            "[copy] Refusing wallet activity because its durable outbox entry is missing."
                        )
                        task.acknowledge(False)
                        continue
                    outbox_entry.attempts += 1
                    outbox_entry.updated_at = int(time.time())

                    def before_live_dispatch(
                        dispatch: Dict[str, Any],
                        _outbox_entry: CopyActivityOutboxEntry = outbox_entry,
                    ) -> None:
                        previous = (
                            _outbox_entry.state,
                            _outbox_entry.outcome_code,
                            _outbox_entry.outcome_message,
                            dict(_outbox_entry.dispatch),
                            _outbox_entry.updated_at,
                        )
                        _set_copy_activity_outcome(
                            _outbox_entry,
                            CopyActivityOutcome(
                                "ambiguous",
                                "live_dispatch_started",
                                "A live dispatch was initiated; reconcile venue order history before retrying.",
                                dispatch,
                            ),
                        )
                        try:
                            # This commit is the safety boundary. The exchange
                            # call must not start unless the non-replayable
                            # dispatch intent is durable first.
                            save_config(self.cfg)
                        except Exception:
                            (
                                _outbox_entry.state,
                                _outbox_entry.outcome_code,
                                _outbox_entry.outcome_message,
                                previous_dispatch,
                                _outbox_entry.updated_at,
                            ) = previous
                            _outbox_entry.dispatch = previous_dispatch
                            raise

                    try:
                        outcome = self._handle_wallet_activity(
                            task.watch_id,
                            task.activity,
                            market_id=outbox_entry.market_id,
                            execution_policy=outbox_entry.execution_policy,
                            staged_at=outbox_entry.created_at,
                            replay_authorized_at=outbox_entry.replay_authorized_at,
                            before_live_dispatch=before_live_dispatch,
                        )
                        if not isinstance(outcome, CopyActivityOutcome):
                            outcome = CopyActivityOutcome(
                                "completed",
                                "handled",
                                "Wallet activity handling completed.",
                            )
                    except Exception as exc:
                        if outbox_entry.state == "ambiguous":
                            outcome = CopyActivityOutcome(
                                "ambiguous",
                                outbox_entry.outcome_code or "live_dispatch_ambiguous",
                                outbox_entry.outcome_message
                                or "A live dispatch may have occurred; manual reconciliation is required.",
                                dict(outbox_entry.dispatch),
                            )
                            self.log(
                                "[copy] Wallet activity handling failed after the live dispatch boundary "
                                f"({type(exc).__name__}); manual reconciliation remains required."
                            )
                        else:
                            outcome = CopyActivityOutcome(
                                "retryable",
                                "pre_dispatch_handler_failure",
                                "Handling failed before a confirmed live dispatch and may be retried.",
                            )
                            self.log(
                                "[copy] Wallet activity handling failed before dispatch "
                                f"({type(exc).__name__}); it remains retryable."
                            )
                    _set_copy_activity_outcome(outbox_entry, outcome)
                    _prune_copy_activity_outbox(self.cfg)
                    try:
                        save_config(self.cfg)
                    except Exception as exc:
                        self.log(
                            "[copy] Copy activity outcome could not be persisted "
                            f"({type(exc).__name__}). Its previously durable state remains authoritative."
                        )
                    task.acknowledge(True)
                elif kind == "config_changed":
                    # Persist config (for last_seen state)
                    save_config(self.cfg)
                    self._refresh_wallet_table()
                elif kind == "dep_versions":
                    rows = a or []
                    errors = b or []
                    self._refresh_dependency_table(rows)
                    ts = time.strftime("%H:%M:%S")
                    if errors:
                        self.dep_status_var.set(f"Checked at {ts} with errors.")
                        for err in errors:
                            self.log(f"[deps] {err}")
                    else:
                        self.dep_status_var.set(f"Checked at {ts}.")
                    self.check_versions_btn.configure(state="normal")
                    self._dep_check_running = False
                elif kind == "polymarket_leaderboard":
                    self._leaderboard_loading = False
                    if hasattr(self, "lb_load_btn"):
                        self.lb_load_btn.configure(state="normal")
                    if hasattr(self, "lb_fast_roi_btn"):
                        self.lb_fast_roi_btn.configure(state="normal")
                    if hasattr(self, "lb_cancel_btn"):
                        self.lb_cancel_btn.configure(state="disabled")
                    self._refresh_polymarket_leaderboard_table(a or {})
                elif kind == "polymarket_leaderboard_progress":
                    self._handle_polymarket_leaderboard_progress(a or {})
                elif kind == "polymarket_leaderboard_error":
                    self._leaderboard_loading = False
                    if hasattr(self, "lb_load_btn"):
                        self.lb_load_btn.configure(state="normal")
                    if hasattr(self, "lb_fast_roi_btn"):
                        self.lb_fast_roi_btn.configure(state="normal")
                    if hasattr(self, "lb_cancel_btn"):
                        self.lb_cancel_btn.configure(state="disabled")
                    self._handle_polymarket_leaderboard_error(str(a))
                else:
                    self.log(f"[debug] unknown queue item: {kind}")
        except queue.Empty:
            pass
        finally:
            if not self._shutdown_started:
                self._queue_after_id = self.after(100, self._process_queue)

    def _handle_wallet_activity(
        self,
        watch_id: str,
        item: Dict[str, Any],
        *,
        market_id: str = "",
        execution_policy: Optional[Dict[str, Any]] = None,
        staged_at: int = 0,
        replay_authorized_at: int = 0,
        before_live_dispatch: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> CopyActivityOutcome:
        # Add to activity UI
        ts = int(item.get("timestamp") or 0)
        tss = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        pseudo = item.get("pseudonym") or item.get("name") or ""
        side = item.get("side") or ""
        slug = item.get("slug") or ""
        outcome = item.get("outcome") or ""
        price = safe_float(item.get("price"), None)
        size = safe_float(item.get("size"), None)

        line = f"{tss}  {pseudo}  {side}  {slug}  {outcome}  price={price}  size={size}"
        self.activity_list.insert(0, line)
        # keep list bounded
        if self.activity_list.size() > 200:
            self.activity_list.delete(200, "end")

        self.log(f"[activity] {line}")

        # Copy trade logic
        return self._copy_trade_from_activity(
            item,
            market_id=market_id,
            execution_policy=execution_policy,
            staged_at=staged_at,
            replay_authorized_at=replay_authorized_at,
            before_live_dispatch=before_live_dispatch,
        )


def application_resource_roots() -> List[Path]:
    """Return source and packaged asset roots, preferring the release folder when frozen."""
    roots: List[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    roots.append(Path(__file__).resolve().parent)
    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def tkinter_smoke_payload() -> Dict[str, Any]:
    registry = build_default_registry()
    cfg = AppConfig()
    market_ids = [metadata.market_id for metadata in registry.list_metadata()]
    choices = [market_choice_label(metadata) for metadata in registry.list_metadata()]
    icon_available = any(
        (root / "assets" / "marketsentinel.ico").exists()
        and any((root / relative_path).exists() for relative_path in (Path("assets/marketsentinel.png"), Path("marketsentinel.png")))
        for root in application_resource_roots()
    )
    return {
        "ok": True,
        "app_class": App.__name__,
        "tkinter_base": issubclass(App, tk.Tk),
        "window_title": APP_TITLE,
        "selected_market_id": cfg.selected_market_id,
        "ui_design": cfg.ui_design,
        "ui_designs": list(UI_DESIGN_LABELS.values()),
        "desktop_tabs": [
            "Markets & Alerts",
            "Paper Trading",
            "Market Safety",
            "Wallet Tracker",
            "Polymarket Analytics",
            "Copy Trading",
            "Logs",
            "About",
        ],
        "icon_available": icon_available,
        "market_count": len(market_ids),
        "choice_count": len(choices),
        "all_markets_configured": set(market_ids) == set(cfg.markets),
        "fallback_command": "python app.py",
    }


def tkinter_gui_lifecycle_smoke_payload(*, app_factory=App) -> Dict[str, Any]:
    """Construct and tear down the real Tk widget tree without network workers."""
    app = app_factory(start_background_workers=False)
    expected_tabs = [
        "Markets & Alerts",
        "Paper Trading",
        "Market Safety",
        "Wallet Tracker",
        "Polymarket Analytics",
        "Copy Trading",
        "Logs",
        "About",
    ]
    try:
        app.withdraw()
        app.update_idletasks()
        tabs = [str(app.notebook.tab(tab_id, "text")) for tab_id in app.notebook.tabs()]
        return {
            "ok": app.winfo_exists() == 1 and tabs == expected_tabs,
            "window_title": app.title(),
            "desktop_tabs": tabs,
            "background_workers_started": app._background_workers_started,
        }
    finally:
        app.shutdown()


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--smoke-test" in args:
        print(json.dumps(tkinter_smoke_payload(), sort_keys=True))
        return 0
    if "--gui-smoke-test" in args:
        payload = tkinter_gui_lifecycle_smoke_payload()
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload["ok"] else 1
    if "--web-gui" in args:
        import argparse

        from web_api import DEFAULT_CONFIG_PATH, run_server

        parser = argparse.ArgumentParser(description="Run the packaged React/TypeScript GUI server.")
        parser.add_argument("--web-gui", action="store_true")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        parsed = parser.parse_args(args)
        run_server(parsed.host, parsed.port, parsed.config)
        return 0

    set_windows_app_id(APP_ID)
    try:
        app = App()
    except ConfigLoadError as exc:
        message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        try:
            messagebox.showerror("MarketSentinel configuration error", message)
        except tk.TclError:
            pass
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
