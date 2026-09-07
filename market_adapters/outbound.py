from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

from core.request_control import RequestCancelled, RequestDeadlineExceeded, resolve_with_deadline
from .errors import MarketConfigurationError


OUTBOUND_PRIVATE_ORIGINS_ENV = "MARKET_SENTINEL_OUTBOUND_PRIVATE_ORIGINS"
MAX_OUTBOUND_URL_LENGTH = 4096

# Keep this inventory centralized so configuration surfaces can prevent endpoint
# changes without trying to infer security-sensitive keys from arbitrary names.
OUTBOUND_ENDPOINT_SETTING_KEYS = frozenset(
    {
        "api_base_url",
        "augur_subgraph_url",
        "azuro_api_base_url",
        "azuro_graph_api_url",
        "azuro_ws_url",
        "betfair_account_api_base_url",
        "betfair_api_base_url",
        "betmgm_api_base_url",
        "clob_api_base_url",
        "clob_host",
        "coinbase_prediction_markets_api_base_url",
        "context_api_base_url",
        "crypto_com_predict_api_base_url",
        "dflow_metadata_api_base_url",
        "dflow_solana_rpc_url",
        "dflow_trade_api_base_url",
        "draftkings_predictions_api_base_url",
        "drift_bet_data_api_base_url",
        "evm_rpc_url",
        "fanatics_markets_api_base_url",
        "fanduel_predicts_api_base_url",
        "frenzy_rpc_url",
        "gemini_api_base_url",
        "gnosis_rpc_url",
        "gnosis_subgraph_url",
        "good_judgment_open_api_base_url",
        "good_judgment_open_base_url",
        "graph_api_url",
        "hedgehog_rpc_url",
        "hyperliquid_api_base_url",
        "hypermind_outcomes_url",
        "hypermind_prices_url",
        "ibkr_api_base_url",
        "iem_historical_markets",
        "kalshi_api_base_url",
        "kalshi_via_robinhood_api_base_url",
        "lamas_finance_rpc_url",
        "limitless_api_base_url",
        "limitless_ws_url",
        "manifold_api_base_url",
        "matchbook_api_base_url",
        "matchbook_login_base_url",
        "metaculus_api_base_url",
        "metadao_api_base_url",
        "metadao_solana_rpc_url",
        "myriad_api_base_url",
        "nadex_api_base_url",
        "omen_rpc_url",
        "omen_subgraph_url",
        "opinion_api_base_url",
        "opinion_clob_host",
        "opinion_rpc_url",
        "prdt_rpc_url",
        "predict_fun_api_base_url",
        "predictit_api_base_url",
        "probable_clob_api_base_url",
        "probable_market_api_base_url",
        "prophet_exchange_api_base_url",
        "reality_eth_subgraph_url",
        "robinhood_prediction_markets_api_base_url",
        "scicast_api_base_url",
        "seer_api_base_url",
        "seer_rpc_url",
        "smarkets_api_base_url",
        "solana_rpc_url",
        "space_api_base_url",
        "substrate_rpc_url",
        "sx_bet_api_base_url",
        "sx_bet_ws_url",
        "thales_api_base_url",
        "thales_rpc_url",
        "thales_subgraph_url",
        "trade_api_base_url",
        "trueo_rpc_url",
        "web3_rpc_url",
        "websocket_url",
        "xmarket_api_base_url",
        "xmarket_authenticated_api_base_url",
        "xo_api_base_url",
        "zeitgeist_indexer_url",
        "zeitgeist_pools_indexer_url",
        "zeitgeist_rpc_url",
        "zeitgeist_sdk_indexer_url",
        "zetarium_rpc_url",
    }
)

OUTBOUND_POLICY_SETTING_KEYS = frozenset(
    {
        "hypermind_allow_custom_data_host",
        "iem_allow_custom_data_host",
        "scicast_allow_custom_base_url",
    }
)


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _normalize_hostname(hostname: str) -> str:
    raw = hostname.rstrip(".")
    if not raw or "%" in raw:
        raise MarketConfigurationError("Outbound endpoint hostname is invalid.")
    try:
        return str(_ip_address(raw))
    except ValueError:
        pass
    try:
        normalized = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise MarketConfigurationError("Outbound endpoint hostname is invalid.") from exc
    if not normalized or len(normalized) > 253:
        raise MarketConfigurationError("Outbound endpoint hostname is invalid.")
    for label in normalized.split("."):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise MarketConfigurationError("Outbound endpoint hostname is invalid.")
    return normalized


def _default_port(scheme: str) -> int:
    return 443 if scheme in {"https", "wss"} else 80


def _origin(scheme: str, hostname: str, port: int) -> str:
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port == _default_port(scheme):
        return f"{scheme}://{rendered_host}"
    return f"{scheme}://{rendered_host}:{port}"


def _parse_url(value: Any, *, setting_key: str, kind: str) -> tuple[SplitResult, str, int, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketConfigurationError(f"{setting_key} must be a canonical absolute URL.")
    if (
        len(value) > MAX_OUTBOUND_URL_LENGTH
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise MarketConfigurationError(f"{setting_key} contains an invalid URL.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MarketConfigurationError(f"{setting_key} contains an invalid URL.") from exc
    allowed_schemes = {"https", "http"} if kind == "http" else {"wss", "ws"}
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes or not parsed.netloc or parsed.hostname is None:
        secure_scheme = "https" if kind == "http" else "wss"
        raise MarketConfigurationError(f"{setting_key} must be an absolute {secure_scheme} URL.")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise MarketConfigurationError(f"{setting_key} cannot contain credentials or a fragment.")
    hostname = _normalize_hostname(parsed.hostname)
    effective_port = port if port is not None else _default_port(scheme)
    if effective_port < 1:
        raise MarketConfigurationError(f"{setting_key} contains an invalid URL port.")
    return parsed, hostname, effective_port, _origin(scheme, hostname, effective_port)


def _normalize_private_origin(value: str) -> str:
    parsed, hostname, port, origin = _parse_url(
        value,
        setting_key=OUTBOUND_PRIVATE_ORIGINS_ENV,
        kind="websocket" if value.lower().startswith(("ws://", "wss://")) else "http",
    )
    if parsed.path not in {"", "/"} or parsed.query:
        raise MarketConfigurationError(
            f"{OUTBOUND_PRIVATE_ORIGINS_ENV} entries must be origins without a path or query."
        )
    if parsed.scheme.lower() in {"http", "ws"}:
        try:
            address = _ip_address(hostname)
        except ValueError:
            if hostname != "localhost" and not hostname.endswith(".localhost"):
                raise MarketConfigurationError(
                    f"{OUTBOUND_PRIVATE_ORIGINS_ENV} permits plaintext only for loopback origins."
                ) from None
        else:
            if not address.is_loopback:
                raise MarketConfigurationError(
                    f"{OUTBOUND_PRIVATE_ORIGINS_ENV} permits plaintext only for loopback origins."
                )
    return origin


@dataclass(frozen=True)
class OutboundEndpointPolicy:
    """Immutable outbound network policy loaded outside market configuration."""

    private_origins: frozenset[str] = field(default_factory=frozenset)
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    ) -> "OutboundEndpointPolicy":
        source = os.environ if environ is None else environ
        raw = str(source.get(OUTBOUND_PRIVATE_ORIGINS_ENV, "") or "")
        origins = frozenset(
            _normalize_private_origin(item.strip())
            for item in raw.split(",")
            if item.strip()
        )
        return cls(private_origins=origins, resolver=resolver)


DEFAULT_OUTBOUND_ENDPOINT_POLICY = OutboundEndpointPolicy.from_environment()


def _resolved_addresses(
    hostname: str,
    port: int,
    *,
    policy: OutboundEndpointPolicy,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        records = resolve_with_deadline(policy.resolver, hostname, port, type=socket.SOCK_STREAM)
    except (RequestCancelled, RequestDeadlineExceeded):
        raise
    except (OSError, socket.gaierror) as exc:
        raise MarketConfigurationError(f"Outbound endpoint hostname could not be resolved: {hostname}.") from exc
    addresses = []
    for record in records:
        try:
            sockaddr = record[4]
            addresses.append(_ip_address(str(sockaddr[0])))
        except (IndexError, TypeError, ValueError):
            continue
    if not addresses:
        raise MarketConfigurationError(f"Outbound endpoint hostname could not be resolved: {hostname}.")
    return tuple(addresses)


def validate_outbound_url_with_addresses(
    value: Any,
    *,
    setting_key: str = "outbound_url",
    kind: str = "http",
    base_url: bool = False,
    policy: OutboundEndpointPolicy | None = None,
    resolve_addresses: bool = True,
) -> tuple[str, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
    """Return a canonical safe URL and the addresses validated for it.

    Callers that open a managed socket should use the returned addresses rather
    than resolving the hostname a second time.  Keeping the validated address
    set alongside the canonical URL closes the DNS-rebinding window between
    policy validation and connection establishment.
    """

    if kind not in {"http", "websocket"}:
        raise ValueError("Outbound endpoint kind must be http or websocket.")
    active_policy = policy or OutboundEndpointPolicy.from_environment()
    parsed, hostname, port, origin = _parse_url(value, setting_key=setting_key, kind=kind)
    if base_url and parsed.query:
        raise MarketConfigurationError(f"{setting_key} base URL cannot contain a query.")

    scheme = parsed.scheme.lower()
    allowlisted_private_origin = origin in active_policy.private_origins
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] = ()
    try:
        literal_address = _ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        addresses = (literal_address,)
    elif hostname == "localhost" or hostname.endswith(".localhost"):
        addresses = (ipaddress.ip_address("127.0.0.1"),)
    elif resolve_addresses:
        addresses = _resolved_addresses(hostname, port, policy=active_policy)

    if scheme in {"http", "ws"}:
        if not allowlisted_private_origin or not addresses or not all(address.is_loopback for address in addresses):
            secure_scheme = "https" if kind == "http" else "wss"
            raise MarketConfigurationError(
                f"{setting_key} must use {secure_scheme}; plaintext is allowed only for an explicitly allowlisted loopback origin."
            )

    if addresses and not allowlisted_private_origin:
        if any(not address.is_global for address in addresses):
            raise MarketConfigurationError(
                f"{setting_key} resolves to a non-public network address that is not allowlisted."
            )

    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = rendered_host if port == _default_port(scheme) else f"{rendered_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, "")), addresses


def validate_outbound_url(
    value: Any,
    *,
    setting_key: str = "outbound_url",
    kind: str = "http",
    base_url: bool = False,
    policy: OutboundEndpointPolicy | None = None,
    resolve_addresses: bool = True,
) -> str:
    """Return a canonical safe URL or fail before an outbound connection is made."""

    canonical, _addresses = validate_outbound_url_with_addresses(
        value,
        setting_key=setting_key,
        kind=kind,
        base_url=base_url,
        policy=policy,
        resolve_addresses=resolve_addresses,
    )
    return canonical


def is_outbound_endpoint_setting(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return normalized in (OUTBOUND_ENDPOINT_SETTING_KEYS | OUTBOUND_POLICY_SETTING_KEYS)
