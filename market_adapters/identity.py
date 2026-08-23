from __future__ import annotations

import re
from typing import Optional

from polymarket.util import normalize_wallet

from .errors import MarketConfigurationError


_MANIFOLD_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SOLANA_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SOLANA_INDEX = {character: index for index, character in enumerate(_SOLANA_ALPHABET)}


def _normalize_solana_identity(raw: object) -> Optional[str]:
    """Return a canonical, explicitly tagged Solana wallet identity."""

    value = str(raw or "").strip()
    if value.lower().startswith("solana:"):
        value = value.split(":", 1)[1].strip()
    if not _SOLANA_ADDRESS_RE.fullmatch(value):
        return None
    number = 0
    try:
        for character in value:
            number = number * 58 + _SOLANA_INDEX[character]
    except KeyError:
        return None
    raw_bytes = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    if len((b"\x00" * leading_zeroes) + raw_bytes) != 32:
        return None
    return f"solana:{value}"


def normalize_activity_identity(market_id: str, raw: object) -> Optional[str]:
    """Normalize the identity used by a market's public activity feed.

    EVM venues continue to use canonical lower-case wallet addresses. Manifold
    activity is keyed by a public username, so it must be explicitly prefixed
    to prevent a username from being confused with a wallet or interpolated
    into an unsafe URL path.
    """

    market = str(market_id or "polymarket").strip().lower() or "polymarket"
    value = str(raw or "").strip()
    if market == "manifold":
        prefix = "manifold:"
        if not value.lower().startswith(prefix):
            return None
        username = value[len(prefix) :].strip().lower()
        if not _MANIFOLD_USERNAME_RE.fullmatch(username):
            return None
        return f"{prefix}{username}"
    if market == "metadao":
        return _normalize_solana_identity(value)
    return normalize_wallet(value)


def require_activity_identity(market_id: str, raw: object) -> str:
    """Return a normalized activity identity or a market-specific error."""

    market = str(market_id or "polymarket").strip().lower() or "polymarket"
    normalized = normalize_activity_identity(market, raw)
    if normalized:
        return normalized
    if market == "manifold":
        raise MarketConfigurationError(
            "Manifold activity identity must use the safe manifold:<username> format."
        )
    if market == "metadao":
        raise MarketConfigurationError(
            "MetaDAO activity identity must use a canonical Solana wallet address (optionally prefixed solana:)."
        )
    raise MarketConfigurationError("Activity identity must be a valid 0x wallet/proxyWallet address.")


def activity_identity_hint(market_id: str) -> str:
    """Return the UI input format for a selected market."""

    market = str(market_id or "").strip().lower()
    if market == "manifold":
        return "manifold:<username>"
    if market == "metadao":
        return "solana:<base58 wallet address>"
    return "0x wallet address"
