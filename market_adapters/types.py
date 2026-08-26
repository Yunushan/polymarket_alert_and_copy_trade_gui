from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MarketCapabilities:
    market_discovery: bool = False
    event_listing: bool = False
    price_reading: bool = False
    orderbook_reading: bool = False
    trade_history: bool = False
    candle_history: bool = False
    alerts: bool = False
    paper_trading: bool = False
    live_trading: bool = False
    copy_trading: bool = False
    api_required: bool = False
    credentials_required: bool = False
    kyc_required: bool = False
    region_limited: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "market_discovery": self.market_discovery,
            "event_listing": self.event_listing,
            "price_reading": self.price_reading,
            "orderbook_reading": self.orderbook_reading,
            "trade_history": self.trade_history,
            "candle_history": self.candle_history,
            "alerts": self.alerts,
            "paper_trading": self.paper_trading,
            "live_trading": self.live_trading,
            "copy_trading": self.copy_trading,
            "api_required": self.api_required,
            "credentials_required": self.credentials_required,
            "kyc_required": self.kyc_required,
            "region_limited": self.region_limited,
        }


@dataclass(frozen=True)
class MarketMetadata:
    market_id: str
    display_name: str
    default_enabled: bool = False
    homepage_url: str = ""
    description: str = ""
    capabilities: MarketCapabilities = field(default_factory=MarketCapabilities)


@dataclass(frozen=True)
class MarketEvent:
    market_id: str
    event_id: str
    title: str
    url: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketContract:
    market_id: str
    contract_id: str
    event_id: str
    title: str
    outcome: str = ""
    url: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceSnapshot:
    market_id: str
    contract_id: str
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    midpoint: Optional[float] = None
    source: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    market_id: str
    contract_id: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketTrade:
    """A normalized public trade from a market's documented trade feed."""

    market_id: str
    contract_id: str
    trade_id: str
    side: str
    price: float
    size: float
    timestamp: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketCandle:
    """A normalized OHLCV candle from a market's documented history feed."""

    market_id: str
    contract_id: str
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperOrderRequest:
    market_id: str
    contract_id: str
    side: str
    size: float
    limit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PaperOrderResult:
    market_id: str
    contract_id: str
    accepted: bool
    message: str
    filled_size: float = 0.0
    average_price: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)
