from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from . import clob_auth, clob_rest
from .auth_readiness import build_clob_auth_readiness, validate_sdk_trading_readiness
from .constants import (
    CLOB_API,
    POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER,
    POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED,
    POLYMARKET_LIVE_MUTATION_BLOCKER,
    POLYMARKET_LIVE_MUTATIONS_SUPPORTED,
)

try:
    from py_clob_client_v2 import (
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        BuilderTradeParams,
        ClobClient,
        MarketOrderArgsV2,
        OpenOrderParams,
        OrderArgsV2,
        OrderMarketCancelParams,
        OrderPayload,
        OrderScoringParams,
        OrderType,
        PostOrdersV2Args,
        TradeParams,
    )
except Exception:  # pragma: no cover
    ApiCreds = None  # type: ignore
    ClobClient = None  # type: ignore
    OrderArgsV2 = None  # type: ignore
    OrderType = None  # type: ignore
    MarketOrderArgsV2 = None  # type: ignore
    OrderMarketCancelParams = None  # type: ignore
    OrderPayload = None  # type: ignore
    PostOrdersV2Args = None  # type: ignore
    AssetType = None  # type: ignore
    BalanceAllowanceParams = None  # type: ignore
    BuilderTradeParams = None  # type: ignore
    OpenOrderParams = None  # type: ignore
    OrderScoringParams = None  # type: ignore
    TradeParams = None  # type: ignore


_CLOB_V2_VERSION_PATH = "/version"
_MAX_BATCH_ORDERS = 15
_MAX_CANCEL_ORDER_IDS = 3000
_V2_SIGNED_ORDER_FIELDS = (
    "salt",
    "maker",
    "signer",
    "tokenId",
    "makerAmount",
    "takerAmount",
    "side",
    "signatureType",
    "timestamp",
    "metadata",
    "builder",
    "signature",
)


@dataclass
class TraderConfig:
    private_key: str
    funder_address: Optional[str] = None
    signature_type: int = 0
    chain_id: int = 137
    host: str = CLOB_API
    l2_headers: Optional[Mapping[str, str]] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    allow_api_key_creation: bool = False
    allow_api_key_derivation: bool = False
    authenticated_sdk_reads: bool = False
    bounded_audit: bool = False


def _order_side(side: str) -> Any:
    normalized = str(side or "").strip().upper()
    if normalized == "BUY":
        return "BUY"
    if normalized == "SELL":
        return "SELL"
    raise ValueError("Polymarket order side must be BUY or SELL.")


def _canonical_identifier(value: Any, label: str) -> str:
    candidate = str(value or "")
    if not candidate or candidate != candidate.strip():
        raise ValueError(f"Polymarket {label} must be a non-empty canonical string.")
    if len(candidate) > 256 or any(not character.isprintable() for character in candidate):
        raise ValueError(f"Polymarket {label} must be printable and at most 256 characters.")
    return candidate


def _positive_finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Polymarket {label} must be a finite positive number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Polymarket {label} must be a finite positive number.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"Polymarket {label} must be a finite positive number.")
    return parsed


def _mapping_response(value: Any, operation: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Polymarket {operation} response is not an object.")
    return dict(value)


def _require_v2_server(client: Any, host: str) -> None:
    """Require an explicit server response instead of the SDK's fail-open version helper."""

    getter = getattr(client, "_get", None)
    if not callable(getter):
        raise RuntimeError("Polymarket V2 client does not expose the strict server-version transport.")
    try:
        response = getter(f"{str(host).rstrip('/')}{_CLOB_V2_VERSION_PATH}")
    except Exception as exc:
        raise RuntimeError("Polymarket CLOB server version could not be verified; mutation is blocked.") from exc
    if not isinstance(response, Mapping) or type(response.get("version")) is not int:
        raise RuntimeError("Polymarket CLOB server returned an invalid version response; mutation is blocked.")
    if response["version"] != 2:
        raise RuntimeError(
            f"Polymarket CLOB server version {response['version']!r} is not V2; mutation is blocked."
        )


def _require_v2_signed_order(order: Any) -> Any:
    """Reject legacy/V1 or ambiguous signed objects before the SDK chooses a serializer."""

    missing = [name for name in _V2_SIGNED_ORDER_FIELDS if not hasattr(order, name)]
    timestamp = getattr(order, "timestamp", None)
    signature = getattr(order, "signature", None)
    if missing or not isinstance(timestamp, str) or not timestamp.strip():
        raise RuntimeError("Polymarket SDK did not build an unambiguous V2 signed order; posting is blocked.")
    if not isinstance(signature, str) or not signature.strip():
        raise RuntimeError("Polymarket SDK returned an unsigned V2 order; posting is blocked.")
    return order


def _optional_identifier(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _canonical_identifier(value, label)


def _optional_non_negative_int(value: Any, label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Polymarket {label} must be a non-negative integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Polymarket {label} must be a non-negative integer.") from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError(f"Polymarket {label} must be a non-negative integer.")
    return parsed


class _AuthenticatedReadClient:
    """Narrow direct-REST reader that cannot expose SDK mutation methods."""

    __slots__ = ("__headers",)

    def __init__(self, headers: Mapping[str, str]):
        self.__headers = {str(key): str(value) for key, value in headers.items() if value}
        missing = [name for name in clob_auth.REQUIRED_L2_HEADERS if name not in self.__headers]
        if missing:
            raise ValueError(f"Polymarket CLOB L2 request headers missing: {', '.join(missing)}")

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return clob_auth.get_order(order_id, self.__headers)

    def get_orders(self, **filters: Any) -> Dict[str, Any]:
        return clob_auth.get_orders(self.__headers, **filters)

    def get_trades(self, **filters: Any) -> Dict[str, Any]:
        return clob_auth.get_trades(self.__headers, **filters)

    def get_order_scoring_status(self, order_id: str) -> Dict[str, Any]:
        return clob_auth.get_order_scoring_status(order_id, self.__headers)

    @staticmethod
    def get_builder_trades(builder_code: str, **filters: Any) -> Dict[str, Any]:
        return clob_rest.get_builder_trades(builder_code, **filters)

    def get_address(self) -> str:
        return self.__headers["POLY_ADDRESS"]


class PolymarketTrader:
    """
    Private Polymarket CLOB V2 client wrapper with a fail-closed mutation edge.

    Authenticated reads can use explicit L2 headers through the repository's
    narrow direct REST client or explicitly initialize a private V2 SDK reader
    that signs every request freshly. The SDK is never exposed publicly, and
    API-key creation and derivation each require separate explicit opt-ins.
    """

    __slots__ = (
        "cfg",
        "auth_readiness",
        "__reader",
        "__reader_is_v2_sdk",
        "__mutation_client",
    )

    def __init__(
        self,
        cfg: TraderConfig,
        *,
        reader: Optional[Any] = None,
        mutation_client: Optional[Any] = None,
    ):
        self.cfg = cfg
        self.__reader = reader
        self.__reader_is_v2_sdk = False
        self.__mutation_client = None
        if self.cfg.private_key:
            self.auth_readiness = validate_sdk_trading_readiness(
                private_key=self.cfg.private_key,
                signature_type=self.cfg.signature_type,
                funder_address=self.cfg.funder_address,
                chain_id=self.cfg.chain_id,
                host=self.cfg.host,
            )
        else:
            if int(self.cfg.chain_id) != 137:
                raise ValueError("Polymarket CLOB reads expect Polygon chain id 137.")
            if str(self.cfg.host).rstrip("/") != CLOB_API:
                raise ValueError("Polymarket CLOB reads require the official CLOB API host.")
            self.auth_readiness = build_clob_auth_readiness(
                {
                    "private_key": "",
                    "signature_type": self.cfg.signature_type,
                    "funder_address": self.cfg.funder_address or "",
                },
                environ=dict(self.cfg.l2_headers or {}),
            )

        if not self._mutations_supported():
            # A read-only SDK is constructed only on explicit request and is
            # never retained as a mutation client. API-key creation remains
            # forbidden on this path.
            if self.__reader is None and self.cfg.authenticated_sdk_reads:
                self.__reader = self._init_read_client()
                self.__reader_is_v2_sdk = True
            elif self.__reader is None and self.cfg.l2_headers is not None:
                self.__reader = _AuthenticatedReadClient(self.cfg.l2_headers)
            return

        if mutation_client is not None:
            self.__mutation_client = mutation_client
        else:
            if ClobClient is None:
                raise RuntimeError("py-clob-client-v2 is not installed. Install requirements-live.lock first.")
            self.__mutation_client = self._init_client()
        if self.__reader is None:
            if self.cfg.l2_headers is not None:
                self.__reader = _AuthenticatedReadClient(self.cfg.l2_headers)
            else:
                # The SDK stays private; callers can use only this wrapper's
                # explicitly reviewed read and mutation methods.
                self.__reader = self.__mutation_client
                self.__reader_is_v2_sdk = True

    def _explicit_api_creds(self) -> Any:
        if ApiCreds is None:
            raise RuntimeError("py-clob-client-v2 is not installed. Install requirements-live.lock first.")
        credential_values = (
            str(self.cfg.api_key or "").strip(),
            str(self.cfg.api_secret or "").strip(),
            str(self.cfg.api_passphrase or "").strip(),
        )
        if any(credential_values) and not all(credential_values):
            raise ValueError(
                "Polymarket V2 API credentials require api_key, api_secret, and api_passphrase together."
            )
        if not all(credential_values):
            return None
        return ApiCreds(
            api_key=credential_values[0],
            api_secret=credential_values[1],
            api_passphrase=credential_values[2],
        )

    def _new_sdk_client(self, creds: Any) -> Any:
        if ClobClient is None:
            raise RuntimeError("py-clob-client-v2 is not installed. Install requirements-live.lock first.")
        return ClobClient(
            self.cfg.host,
            chain_id=self.cfg.chain_id,
            key=self.cfg.private_key,
            creds=creds,
            signature_type=self.cfg.signature_type,
            funder=self.cfg.funder_address,
            retry_on_error=False,
        )

    def _set_api_creds(self, client: Any, creds: Any) -> None:
        setter = getattr(client, "set_api_creds", None)
        if not callable(setter):
            raise RuntimeError("py-clob-client-v2 cannot accept explicit API credentials.")
        setter(creds)

    def _init_read_client(self) -> Any:
        if not self.cfg.private_key:
            raise ValueError("Authenticated Polymarket V2 SDK reads require an explicit private key.")
        if self.cfg.allow_api_key_creation:
            raise ValueError("Read-only Polymarket V2 SDK initialization forbids API-key creation.")
        creds = self._explicit_api_creds()
        if creds is None and not self.cfg.allow_api_key_derivation:
            raise ValueError(
                "Authenticated Polymarket V2 SDK reads require explicit API credentials or "
                "a separate allow_api_key_derivation opt-in."
            )
        client = self._new_sdk_client(creds)
        if creds is None:
            try:
                creds = client.derive_api_key()
            except Exception as exc:
                raise RuntimeError("Unable to derive existing Polymarket V2 API credentials.") from exc
        self._set_api_creds(client, creds)
        return client

    def _init_client(self):
        if not self._mutations_supported():
            raise RuntimeError(self._mutation_blocker())
        if ClobClient is None or ApiCreds is None:
            raise RuntimeError("py-clob-client-v2 is not installed. Install requirements-live.lock first.")

        creds = self._explicit_api_creds()
        client = self._new_sdk_client(creds)

        if creds is None:
            try:
                # Derivation is a signed GET and does not create or rotate an
                # API key. Creation requires a separate explicit opt-in.
                creds = client.derive_api_key()
            except Exception as exc:
                if not self.cfg.allow_api_key_creation:
                    raise RuntimeError(
                        "Unable to derive existing Polymarket V2 API credentials. "
                        "Provide explicit API credentials or separately approve one-time API-key creation."
                    ) from exc
                creds = client.create_or_derive_api_key()
        self._set_api_creds(client, creds)

        return client

    def _mutation(self) -> Any:
        if not self._mutations_supported():
            raise RuntimeError(self._mutation_blocker())
        if self.__mutation_client is None:
            raise RuntimeError("Polymarket V2 mutation client is not initialized.")
        return self.__mutation_client

    def _mutations_supported(self) -> bool:
        if self.cfg.bounded_audit:
            return POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED
        return POLYMARKET_LIVE_MUTATIONS_SUPPORTED

    def _mutation_blocker(self) -> str:
        if self.cfg.bounded_audit:
            return POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER
        return POLYMARKET_LIVE_MUTATION_BLOCKER

    def place_limit_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tif: str = "GTC",
        post_only: bool = False,
    ) -> Dict[str, Any]:
        token = _canonical_identifier(token_id, "token id")
        side_value = _order_side(side)
        normalized_price = _positive_finite(price, "limit price")
        if normalized_price >= 1:
            raise ValueError("Polymarket limit price must be greater than 0 and less than 1.")
        normalized_size = _positive_finite(size, "order size")
        if str(tif or "").strip().upper() != "GTC":
            raise ValueError("Polymarket guarded limit orders require exact TIF=GTC.")
        if not isinstance(post_only, bool):
            raise ValueError("Polymarket post_only must be a boolean.")
        if OrderArgsV2 is None or OrderType is None:
            raise RuntimeError("py-clob-client-v2 is missing V2 limit-order types.")
        client = self._mutation()
        _require_v2_server(client, self.cfg.host)
        order = OrderArgsV2(
            token_id=token,
            price=normalized_price,
            size=normalized_size,
            side=side_value,
        )
        signed_order = _require_v2_signed_order(client.create_order(order))
        response = client.post_order(
            signed_order,
            order_type=OrderType.GTC,
            post_only=post_only,
            defer_exec=False,
        )
        return _mapping_response(response, "limit-order placement")

    def get_trading_balance_allowance(self, *, token_id: str, side: str) -> Dict[str, Any]:
        """Return the official account balance/allowance response for the order asset."""

        normalized_side = str(side or "").strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("Polymarket order side must be BUY or SELL.")
        token = _canonical_identifier(token_id, "token id")
        client = self._mutation()
        if AssetType is None or BalanceAllowanceParams is None:
            raise RuntimeError("py-clob-client-v2 is missing balance/allowance types.")
        if normalized_side == "BUY":
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.cfg.signature_type,
            )
        else:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token,
                signature_type=self.cfg.signature_type,
            )
        response = client.get_balance_allowance(params)
        return _mapping_response(response, "balance/allowance")

    def get_trading_account_address(self) -> str:
        """Return the account identity used by this exact trading client."""

        if self.cfg.funder_address:
            return str(self.cfg.funder_address)
        address = self._call_client(("get_address",)) if self.__reader is not None else self._mutation().get_address()
        if not isinstance(address, str) or not address.strip():
            raise RuntimeError("Polymarket client did not expose a trading account address.")
        return address.strip()

    def place_market_order_amount(
        self,
        *,
        token_id: str,
        side: str,
        amount: float,
        tif: str = "FOK",
    ) -> Dict[str, Any]:
        token = _canonical_identifier(token_id, "token id")
        side_value = _order_side(side)
        normalized_amount = _positive_finite(amount, "market-order amount")
        if str(tif or "").strip().upper() != "FOK":
            raise ValueError("Polymarket guarded market orders require exact TIF=FOK.")
        if MarketOrderArgsV2 is None or OrderType is None:
            raise RuntimeError("py-clob-client-v2 is missing V2 market-order types.")
        client = self._mutation()
        _require_v2_server(client, self.cfg.host)
        order = MarketOrderArgsV2(
            token_id=token,
            amount=normalized_amount,
            side=side_value,
            order_type=OrderType.FOK,
        )
        signed_order = _require_v2_signed_order(client.create_market_order(order))
        response = client.post_order(
            signed_order,
            order_type=OrderType.FOK,
            post_only=False,
            defer_exec=False,
        )
        return _mapping_response(response, "market-order placement")

    def _uses_v2_sdk_reader(self) -> bool:
        return self.__reader_is_v2_sdk

    def _call_client(self, method_names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        if self.__reader is None:
            raise RuntimeError(
                "Authenticated Polymarket reads require explicit L2 headers; "
                "legacy SDK credential derivation is disabled during the CLOB V2 migration."
            )
        last_error: Optional[Exception] = None
        for method_name in method_names:
            fn = getattr(self.__reader, method_name, None)
            if not callable(fn):
                continue
            try:
                return fn(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        names = ", ".join(method_names)
        if last_error is not None:
            raise RuntimeError(f"Polymarket client method signature mismatch for: {names}") from last_error
        raise RuntimeError(f"Polymarket client does not expose any of: {names}")

    def get_order(self, order_id: str) -> Dict[str, Any]:
        identifier = _canonical_identifier(order_id, "order id")
        if self._uses_v2_sdk_reader():
            return _mapping_response(self.__reader.get_order(identifier), "authenticated order read")
        return _mapping_response(
            self._call_client(("get_order", "get_order_by_id"), identifier),
            "authenticated order read",
        )

    def get_orders(self, **filters: Any) -> Any:
        if not self._uses_v2_sdk_reader():
            return self._call_client(("get_orders",), **filters)
        if OpenOrderParams is None:
            raise RuntimeError("py-clob-client-v2 is missing open-order parameter types.")
        values = dict(filters)
        only_first_page = values.pop("only_first_page", False)
        next_cursor = _optional_identifier(values.pop("next_cursor", None), "next cursor")
        if not isinstance(only_first_page, bool):
            raise ValueError("Polymarket only_first_page must be a boolean.")
        unexpected = sorted(set(values) - {"id", "market", "asset_id"})
        if unexpected:
            raise ValueError(f"Unsupported Polymarket V2 open-order filters: {', '.join(unexpected)}")
        params = OpenOrderParams(
            id=_optional_identifier(values.get("id"), "order id"),
            market=_optional_identifier(values.get("market"), "market id"),
            asset_id=_optional_identifier(values.get("asset_id"), "asset id"),
        )
        response = self.__reader.get_open_orders(
            params=params,
            only_first_page=only_first_page,
            next_cursor=next_cursor,
        )
        if not isinstance(response, list):
            raise RuntimeError("Polymarket authenticated open-order response is not a list.")
        return response

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        identifier = _canonical_identifier(order_id, "order id")
        client = self._mutation()
        if OrderPayload is None:
            raise RuntimeError("py-clob-client-v2 is missing cancellation types.")
        response = client.cancel_order(OrderPayload(orderID=identifier))
        return _mapping_response(response, "single-order cancellation")

    def cancel_orders(self, order_ids: Iterable[str]) -> Dict[str, Any]:
        identifiers = []
        for order_id in order_ids:
            identifier = _canonical_identifier(order_id, "order id")
            if identifier not in identifiers:
                identifiers.append(identifier)
                if len(identifiers) > _MAX_CANCEL_ORDER_IDS:
                    raise ValueError(
                        f"Polymarket multi-order cancellation accepts at most {_MAX_CANCEL_ORDER_IDS} order ids."
                    )
        if not identifiers:
            raise ValueError("Polymarket multi-order cancellation requires at least one order id.")
        response = self._mutation().cancel_orders(identifiers)
        return _mapping_response(response, "multi-order cancellation")

    def cancel_all_orders(self) -> Dict[str, Any]:
        response = self._mutation().cancel_all()
        return _mapping_response(response, "cancel-all")

    def cancel_market_orders(self, condition_id: str, *, asset_id: Optional[str] = None) -> Dict[str, Any]:
        market = _canonical_identifier(condition_id, "condition id")
        asset = _canonical_identifier(asset_id, "asset id") if asset_id is not None else None
        client = self._mutation()
        if OrderMarketCancelParams is None:
            raise RuntimeError("py-clob-client-v2 is missing market-cancellation types.")
        response = client.cancel_market_orders(
            OrderMarketCancelParams(market=market, asset_id=asset)
        )
        return _mapping_response(response, "market-order cancellation")

    def place_multiple_orders(self, signed_orders: Iterable[Any], tif: str = "GTC") -> Any:
        if str(tif or "").strip().upper() != "GTC":
            raise ValueError("Polymarket guarded limit orders require exact TIF=GTC.")
        if PostOrdersV2Args is None or OrderType is None:
            raise RuntimeError("py-clob-client-v2 is missing batch-order types.")
        payloads = []
        for item in signed_orders:
            if len(payloads) >= _MAX_BATCH_ORDERS:
                raise ValueError(f"Polymarket batch placement accepts at most {_MAX_BATCH_ORDERS} orders.")
            if isinstance(item, PostOrdersV2Args):
                if item.orderType != OrderType.GTC or bool(item.deferExec):
                    raise ValueError("Polymarket batch placement requires exact orderType=GTC and deferExec=false.")
                signed_order = item.order
                payload = item
            else:
                signed_order = item
                payload = PostOrdersV2Args(order=item, orderType=OrderType.GTC, deferExec=False)
            _require_v2_signed_order(signed_order)
            payloads.append(payload)
        if not payloads:
            raise ValueError("Polymarket batch placement requires at least one signed order.")
        client = self._mutation()
        _require_v2_server(client, self.cfg.host)
        response = client.post_orders(payloads, post_only=False, defer_exec=False)
        if not isinstance(response, (Mapping, list)):
            raise RuntimeError("Polymarket batch-order placement response must be an object or list.")
        return dict(response) if isinstance(response, Mapping) else response

    def get_trades(self, **filters: Any) -> Any:
        if not self._uses_v2_sdk_reader():
            return self._call_client(("get_trades",), **filters)
        if TradeParams is None:
            raise RuntimeError("py-clob-client-v2 is missing trade parameter types.")
        values = dict(filters)
        only_first_page = values.pop("only_first_page", False)
        next_cursor = _optional_identifier(values.pop("next_cursor", None), "next cursor")
        if not isinstance(only_first_page, bool):
            raise ValueError("Polymarket only_first_page must be a boolean.")
        expected = {"id", "maker_address", "market", "asset_id", "before", "after"}
        unexpected = sorted(set(values) - expected)
        if unexpected:
            raise ValueError(f"Unsupported Polymarket V2 trade filters: {', '.join(unexpected)}")
        params = TradeParams(
            id=_optional_identifier(values.get("id"), "trade id"),
            maker_address=_optional_identifier(values.get("maker_address"), "maker address"),
            market=_optional_identifier(values.get("market"), "market id"),
            asset_id=_optional_identifier(values.get("asset_id"), "asset id"),
            before=_optional_non_negative_int(values.get("before"), "before cursor"),
            after=_optional_non_negative_int(values.get("after"), "after cursor"),
        )
        response = self.__reader.get_trades(
            params=params,
            only_first_page=only_first_page,
            next_cursor=next_cursor,
        )
        if not isinstance(response, list):
            raise RuntimeError("Polymarket authenticated trade response is not a list.")
        return response

    def get_order_scoring_status(self, order_id: str) -> Any:
        identifier = _canonical_identifier(order_id, "order id")
        if self._uses_v2_sdk_reader():
            if OrderScoringParams is None:
                raise RuntimeError("py-clob-client-v2 is missing order-scoring parameter types.")
            return _mapping_response(
                self.__reader.is_order_scoring(OrderScoringParams(orderId=identifier)),
                "order-scoring read",
            )
        return self._call_client(("get_order_scoring_status", "get_order_status"), identifier)

    def send_heartbeat(self, heartbeat_id: Optional[str] = None) -> Any:
        identifier = "" if heartbeat_id is None else _canonical_identifier(heartbeat_id, "heartbeat id")
        response = _mapping_response(
            self._mutation().post_heartbeat(identifier),
            "heartbeat",
        )
        returned_id = response.get("heartbeat_id")
        if not isinstance(returned_id, str) or not returned_id.strip():
            raise RuntimeError("Polymarket heartbeat response did not contain a reusable heartbeat id.")
        return response

    def get_builder_trades(self, builder_code: str, **filters: Any) -> Any:
        builder = _canonical_identifier(builder_code, "builder code")
        if self._uses_v2_sdk_reader():
            if BuilderTradeParams is None:
                raise RuntimeError("py-clob-client-v2 is missing builder-trade parameter types.")
            values = dict(filters)
            next_cursor = _optional_identifier(values.pop("next_cursor", None), "next cursor")
            expected = {"id", "maker_address", "market", "asset_id", "before", "after"}
            unexpected = sorted(set(values) - expected)
            if unexpected:
                raise ValueError(f"Unsupported Polymarket V2 builder-trade filters: {', '.join(unexpected)}")
            params = BuilderTradeParams(
                builder_code=builder,
                id=_optional_identifier(values.get("id"), "trade id"),
                maker_address=_optional_identifier(values.get("maker_address"), "maker address"),
                market=_optional_identifier(values.get("market"), "market id"),
                asset_id=_optional_identifier(values.get("asset_id"), "asset id"),
                before=_optional_identifier(values.get("before"), "before cursor"),
                after=_optional_identifier(values.get("after"), "after cursor"),
            )
            return _mapping_response(
                self.__reader.get_builder_trades(params, next_cursor=next_cursor),
                "builder-trade read",
            )
        params: Dict[str, Any] = {"builder_code": builder_code}
        params.update(filters)
        return self._call_client(("get_builder_trades",), **params)
