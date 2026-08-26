from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Literal, Optional, Dict, Any, List, cast
import uuid
import time

from market_adapters.catalog import MARKET_CATALOG


PriceSource = Literal["last_trade", "midpoint", "best_bid", "best_ask"]
Direction = Literal["above", "below"]
Theme = Literal["light", "dark"]
UIDesign = Literal["classic", "aurora_2026", "graphite_2026", "sentinel_2027"]
DEFAULT_MARKET_ID = "polymarket"
DEFAULT_UI_DESIGN: UIDesign = "aurora_2026"


def _uuid() -> str:
    return str(uuid.uuid4())


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
        data["accepted"] = bool(data.get("accepted", False))
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
        return WalletWatch(**d)


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
        text = str(value or "").strip()
        if text.lower().startswith("solana:"):
            return "solana:" + text.split(":", 1)[1].strip()
        return text.lower()

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["follow_wallets"] = self.normalized_follow_wallets()
        data["follow_wallet"] = data["follow_wallets"][0] if data["follow_wallets"] else ""
        data["copy_percentage"] = round(max(0.0, min(float(self.scale), 1.0)) * 100.0, 10)
        return data

    def normalized_follow_wallets(self) -> List[str]:
        wallets: List[str] = []
        for value in [self.follow_wallet, *(self.follow_wallets or [])]:
            wallet = self._stored_identity(value)
            if wallet and wallet not in wallets:
                wallets.append(wallet)
        return wallets

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CopyTradeSettings":
        data = dict(d or {})
        raw_percentage = data.pop("copy_percentage", None)
        if raw_percentage not in (None, ""):
            try:
                data["scale"] = float(raw_percentage) / 100.0
            except (TypeError, ValueError):
                data["scale"] = 1.0
        try:
            scale = float(data.get("scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        data["scale"] = max(0.0, min(scale, 1.0))
        raw_wallets = data.get("follow_wallets", [])
        if isinstance(raw_wallets, str):
            raw_wallets = raw_wallets.replace(";", ",").split(",")
        if not isinstance(raw_wallets, list):
            raw_wallets = []
        wallets: List[str] = []
        for value in [data.get("follow_wallet", ""), *raw_wallets]:
            wallet = CopyTradeSettings._stored_identity(value)
            if wallet and wallet not in wallets:
                wallets.append(wallet)
        data["follow_wallet"] = wallets[0] if wallets else ""
        data["follow_wallets"] = wallets
        try:
            window = int(data.get("conflict_window_seconds", 300))
        except (TypeError, ValueError):
            window = 300
        data["conflict_window_seconds"] = max(0, min(window, 86400))
        data["conflict_guard"] = bool(data.get("conflict_guard", True))
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
            enabled=bool(d.get("enabled", False)),
            settings=dict(settings),
        )


def default_market_configs() -> Dict[str, MarketConfig]:
    return {
        meta.market_id: MarketConfig(market_id=meta.market_id, enabled=meta.default_enabled)
        for meta in MARKET_CATALOG
    }


@dataclass
class AppConfig:
    alerts: List[PriceAlert] = field(default_factory=list)
    paper_trades: List[PaperTradeRecord] = field(default_factory=list)
    wallets: List[WalletWatch] = field(default_factory=list)
    copytrading: CopyTradeSettings = field(default_factory=CopyTradeSettings)
    markets: Dict[str, MarketConfig] = field(default_factory=default_market_configs)
    selected_market_id: str = DEFAULT_MARKET_ID
    theme: Theme = "light"
    ui_design: UIDesign = DEFAULT_UI_DESIGN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alerts": [a.to_dict() for a in self.alerts],
            "paper_trades": [t.to_dict() for t in self.paper_trades],
            "wallets": [w.to_dict() for w in self.wallets],
            "copytrading": self.copytrading.to_dict(),
            "markets": {market_id: cfg.to_dict() for market_id, cfg in self.markets.items()},
            "selected_market_id": self.selected_market_id,
            "theme": self.theme,
            "ui_design": self.ui_design,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AppConfig":
        alerts = [PriceAlert.from_dict(x) for x in d.get("alerts", [])]
        paper_trades = [PaperTradeRecord.from_dict(x) for x in d.get("paper_trades", [])]
        wallets = [WalletWatch.from_dict(x) for x in d.get("wallets", [])]
        copytrading = CopyTradeSettings.from_dict(d.get("copytrading", {}))
        markets = default_market_configs()
        raw_markets = d.get("markets", {})
        if isinstance(raw_markets, dict):
            for market_id, raw_cfg in raw_markets.items():
                if isinstance(raw_cfg, dict):
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
            copytrading=copytrading,
            markets=markets,
            selected_market_id=selected_market_id,
            theme=theme,
            ui_design=ui_design,
        )
