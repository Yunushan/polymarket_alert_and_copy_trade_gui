from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .errors import MarketConfigurationError, UnsupportedFeatureError
from .runtime import AdapterRuntime, ResolvedCredential
from .types import (
    MarketCapabilities,
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookSnapshot,
    MarketTrade,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


class MarketAdapter:
    """Base class for prediction-market adapters.

    Concrete adapters should override only the operations their market supports.
    Unsupported operations raise clear adapter errors by default.
    """

    metadata = MarketMetadata(market_id="base", display_name="Base")
    live_order_sides = ("BUY", "SELL")
    # The shared cap is deliberately conservative: for binary contracts a
    # submitted share quantity is also the maximum face-value exposure, while
    # stake/budget venues already express ``size`` as collateral. Adapters
    # whose liability can exceed size (for example exchange LAY bets) must
    # override ``live_order_exposure``.
    live_order_exposure_model = "full_size_upper_bound"
    # Account recovery is deliberately separate from public trade history.
    # Adapters opt in only when the upstream account endpoints are documented
    # and the implementation has explicit credential/safety tests.
    account_recovery_operations: tuple[str, ...] = ()
    # Mutating order-management operations are deliberately separate from
    # account recovery reads.  Concrete adapters must publish an explicit
    # allow-list and enforce their own documented request schema and safety
    # gates before sending a request.
    order_management_operations: tuple[str, ...] = ()

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        runtime: Optional[AdapterRuntime] = None,
    ) -> None:
        self.config: Dict[str, Any] = dict(config or {})
        self.runtime = runtime or self._create_runtime()

    @property
    def market_id(self) -> str:
        return self.metadata.market_id

    @property
    def display_name(self) -> str:
        return self.metadata.display_name

    @property
    def capabilities(self) -> MarketCapabilities:
        return self.metadata.capabilities

    def _create_runtime(self) -> AdapterRuntime:
        return AdapterRuntime(self.market_id, self.config)

    def health_check(self) -> Dict[str, Any]:
        return {
            "market_id": self.market_id,
            "ok": True,
            "message": "Adapter loaded.",
            "adapter": type(self).__name__,
            "capabilities": self.capabilities.to_dict(),
            "runtime": self.runtime.describe(),
        }

    def ensure_capability(self, capability: str) -> None:
        if not getattr(self.capabilities, capability, False):
            raise UnsupportedFeatureError(self.market_id, capability)

    def config_bool(self, key: str, default: bool = False) -> bool:
        return self.runtime.config_bool(key, default)

    def config_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        return self.runtime.config_float(key, default)

    def resolve_credential(
        self,
        config_key: str,
        env_vars: Iterable[str] = (),
        *,
        required: bool = False,
        label: str = "",
    ) -> Optional[ResolvedCredential]:
        return self.runtime.resolve_credential(config_key, env_vars, required=required, label=label)

    def ensure_order_market(self, order: PaperOrderRequest) -> None:
        if order.market_id != self.market_id:
            raise MarketConfigurationError(f"Order market mismatch: {order.market_id}")

    def ensure_live_trading_enabled(self, feature_name: str = "live trading") -> None:
        if not self.config_bool("live_trading_enabled", False):
            raise MarketConfigurationError(f"{self.display_name} {feature_name} is disabled by adapter config.")
        if self.config_bool("live_trading_kill_switch", False) or self.config_bool("live_trading_paused", False):
            raise MarketConfigurationError(f"{self.display_name} {feature_name} is blocked by the live trading kill switch.")
        if not (
            self.config_bool("live_trading_confirmed", False)
            or self.config_bool("live_trading_acknowledged", False)
        ):
            raise MarketConfigurationError(
                f"{self.display_name} {feature_name} requires explicit acknowledgement. "
                "Set live_trading_confirmed=true only after reviewing live-order risk controls."
            )

    def preflight_live_order(
        self,
        order: PaperOrderRequest,
        *,
        feature_name: str = "live trading",
    ) -> Dict[str, Any]:
        """Run the shared live-order safety checks and return a redacted audit payload."""

        self.ensure_capability("live_trading")
        self.ensure_order_market(order)
        self.ensure_live_trading_enabled(feature_name)

        contract_id = str(order.contract_id or "")
        if not contract_id.strip() or contract_id != contract_id.strip():
            raise MarketConfigurationError(f"{self.display_name} live order requires a non-empty canonical contract id.")
        side = str(order.side or "")
        allowed_sides = tuple(str(value) for value in self.live_order_sides)
        if side not in allowed_sides:
            raise MarketConfigurationError(
                f"{self.display_name} live order side must be one of: {', '.join(allowed_sides)}."
            )

        size = self._finite_float(order.size, "order size")
        if size <= 0:
            raise MarketConfigurationError(f"{self.display_name} live order size must be positive.")

        limit_price = None
        if order.limit_price is not None:
            limit_price = self._finite_float(order.limit_price, "limit price")
            if limit_price <= 0:
                raise MarketConfigurationError(f"{self.display_name} live order limit price must be positive.")

        approx_notional = self._finite_float(
            self.live_order_exposure(order, size=size, limit_price=limit_price),
            "maximum exposure",
        )
        if approx_notional < 0:
            raise MarketConfigurationError(f"{self.display_name} live order exposure must not be negative.")
        max_size = self._positive_config_float("live_trading_max_size")
        max_notional = self._positive_config_float("live_trading_max_notional")

        if max_size is not None and size > max_size:
            raise MarketConfigurationError(
                f"{self.display_name} live order size {size:g} exceeds configured max {max_size:g}."
            )
        if max_notional is not None and approx_notional > max_notional:
            raise MarketConfigurationError(
                f"{self.display_name} live order notional {approx_notional:g} exceeds configured max {max_notional:g}."
            )

        warnings = []
        if self.capabilities.credentials_required:
            warnings.append("credentials_required")
        if self.capabilities.kyc_required:
            warnings.append("kyc_required")
        if self.capabilities.region_limited:
            warnings.append("region_limited")

        preview = f"Would submit live {side} order for {size:g} on {self.display_name} contract {order.contract_id}"
        if limit_price is not None:
            preview += f" at limit {limit_price:g}"

        return {
            "market_id": self.market_id,
            "display_name": self.display_name,
            "feature": feature_name,
            "contract_id": contract_id,
            "side": side,
            "size": size,
            "limit_price": limit_price,
            "approx_notional": approx_notional,
            "exposure_model": self.live_order_exposure_model,
            "max_size": max_size,
            "max_notional": max_notional,
            "live_trading_enabled": True,
            "confirmed": True,
            "kill_switch": False,
            "paper_trading_supported": bool(self.capabilities.paper_trading),
            "requires_credentials": bool(self.capabilities.credentials_required),
            "requires_kyc": bool(self.capabilities.kyc_required),
            "region_limited": bool(self.capabilities.region_limited),
            "warnings": warnings,
            "metadata_keys": sorted(str(key) for key in order.metadata.keys()),
            "dry_run_preview": preview,
        }

    def live_order_exposure(
        self,
        order: PaperOrderRequest,
        *,
        size: float,
        limit_price: Optional[float],
    ) -> float:
        """Return a conservative collateral/maximum-loss bound for preflight.

        The default intentionally charges at least the full submitted size
        instead of the lower ``size * probability`` transaction notional. If
        a venue uses prices above one, it also covers ``size * limit_price``.
        This prevents market orders and naked SELL orders from bypassing the
        configured live-trading cap. Venue-specific liability models may
        return a larger value.
        """

        del order
        return size * max(1.0, limit_price or 0.0)

    def _positive_config_float(self, key: str) -> Optional[float]:
        value = self.config_float(key, None)
        if value is None:
            return None
        if value <= 0:
            raise MarketConfigurationError(f"{self.market_id} config {key} must be greater than 0.")
        return value

    @staticmethod
    def _finite_float(value: Any, label: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketConfigurationError(f"Live order {label} must be numeric.") from exc
        if not math.isfinite(number):
            raise MarketConfigurationError(f"Live order {label} must be finite.")
        return number

    def ensure_copy_trading_enabled(self, feature_name: str = "copy trading") -> None:
        if not self.config_bool("copy_trading_enabled", False):
            raise MarketConfigurationError(f"{self.display_name} {feature_name} is disabled by adapter config.")

    def list_events(self, query: str = "", limit: int = 50) -> List[MarketEvent]:
        self.ensure_capability("event_listing")
        raise UnsupportedFeatureError(self.market_id, "event_listing")

    def list_contracts(self, event_id: str) -> List[MarketContract]:
        self.ensure_capability("event_listing")
        raise UnsupportedFeatureError(self.market_id, "event_listing")

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.ensure_capability("price_reading")
        raise UnsupportedFeatureError(self.market_id, "price_reading")

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.ensure_capability("orderbook_reading")
        raise UnsupportedFeatureError(self.market_id, "orderbook_reading")

    def list_trades(
        self,
        contract_id: str,
        *,
        limit: int = 50,
        before: Optional[float] = None,
        after: Optional[float] = None,
    ) -> List[MarketTrade]:
        """Return normalized public trades when an adapter documents that feed."""

        self.ensure_capability("trade_history")
        raise UnsupportedFeatureError(self.market_id, "trade_history")

    def list_candles(
        self,
        contract_id: str,
        *,
        resolution: str = "1h",
        from_timestamp: Optional[float] = None,
        to_timestamp: Optional[float] = None,
    ) -> List[MarketCandle]:
        """Return normalized OHLCV history when an adapter documents that feed."""

        self.ensure_capability("candle_history")
        raise UnsupportedFeatureError(self.market_id, "candle_history")

    def account_recovery(self, operation: str, **kwargs: Any) -> Any:
        """Read a documented authenticated account surface, when supported.

        This is intentionally not part of the public market-data capability
        flags: account payloads are private, credentialed, and vary by venue.
        Concrete adapters must publish an explicit operation allow-list and
        validate each operation's parameters before making a request.
        """

        del operation, kwargs
        raise UnsupportedFeatureError(self.market_id, "account_recovery")

    def manage_orders(self, operation: str, **kwargs: Any) -> Any:
        """Run a documented authenticated order-management mutation."""

        del operation, kwargs
        raise UnsupportedFeatureError(self.market_id, "order_management")

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.ensure_capability("paper_trading")
        raise UnsupportedFeatureError(self.market_id, "paper_trading")

    def place_live_order(self, order: PaperOrderRequest) -> Dict[str, Any]:
        self.ensure_capability("live_trading")
        raise UnsupportedFeatureError(
            self.market_id,
            "live_trading",
            f"{self.display_name} live trading is not implemented in this adapter.",
        )

    def copy_trade_from_activity(self, activity: Mapping[str, Any]) -> PaperOrderResult:
        self.ensure_capability("copy_trading")
        raise UnsupportedFeatureError(self.market_id, "copy_trading")
