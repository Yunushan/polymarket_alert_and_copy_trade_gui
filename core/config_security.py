from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Iterator, Tuple
from urllib.parse import urlsplit


class ConfigSecurityError(ValueError):
    """Raised when configuration data would persist credential material."""


_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENTIFIER = re.compile(r"[^a-z0-9]+")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_JWT_VALUE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")

# These names describe references to credentials rather than credential values.
_SAFE_REFERENCE_KEYS = {
    "credential_env_vars",
    "required_credentials",
}

_SENSITIVE_CONTAINER_KEYS = {
    "auth_headers",
    "authorization_headers",
    "credentials",
    "credential_sources",
    "credential_values",
    "polymarket_l2_headers",
}

# Keep this list intentionally narrower than a generic ``token``/``private``
# substring check.  Market and contract identifiers legitimately use names such
# as ``token_id`` and ``private_market``.
_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "api_secret",
    "api_token",
    "app_key",
    "access_key",
    "auth_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "cookie",
    "id_token",
    "jwt",
    "mfa_code",
    "passphrase",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "session_cookie",
    "session_token",
    "signature",
    "signing_key",
    "access_token",
}

_SENSITIVE_SUFFIXES = tuple(f"_{name}" for name in _SENSITIVE_EXACT_KEYS)
_SENSITIVE_CONTAINS = (
    "_api_key_",
    "_api_secret_",
    "_api_token_",
    "_app_key_",
    "_access_key_",
    "_secret_key_",
    "_private_key_",
    "_signing_key_",
    "_session_cookie_",
    "_session_token_",
    "_access_token_",
    "_auth_token_",
    "_bearer_token_",
    "_refresh_token_",
    "_client_secret_",
)

# Adapter-specific credential names whose generic suffix is also used for
# ordinary market identifiers elsewhere in the application.
_SENSITIVE_ADAPTER_KEYS = {
    "limitless_token_id",
    "predict_fun_jwt",
    "betmgm_access_id",
}


def normalize_config_key(key: object) -> str:
    text = _CAMEL_CASE_BOUNDARY.sub("_", str(key or "").strip())
    return _NON_IDENTIFIER.sub("_", text.lower()).strip("_")


def _is_reference_key(normalized: str) -> bool:
    return normalized in _SAFE_REFERENCE_KEYS or normalized.endswith(("_env", "_env_var", "_env_vars", "_path"))


def _is_sensitive_normalized_key(normalized: str) -> bool:
    if normalized in _SENSITIVE_ADAPTER_KEYS or normalized in _SENSITIVE_EXACT_KEYS:
        return True
    if normalized.endswith(_SENSITIVE_SUFFIXES):
        return True
    return any(fragment in f"_{normalized}_" for fragment in _SENSITIVE_CONTAINS)


def is_sensitive_config_key(key: object) -> bool:
    """Return whether *key* conventionally stores credential material.

    Environment-variable names and filesystem paths are references, so they
    remain safe to persist even when the referenced credential is sensitive.
    """

    normalized = normalize_config_key(key)
    if not normalized or _is_reference_key(normalized):
        return False
    return _is_sensitive_normalized_key(normalized)


def is_sensitive_display_key(key: object) -> bool:
    """Return whether a field should be redacted from API/audit output."""

    normalized = normalize_config_key(key)
    if not normalized or normalized in _SAFE_REFERENCE_KEYS:
        return False
    if normalized in _SENSITIVE_CONTAINER_KEYS:
        return True
    if normalized.endswith(("_env", "_env_var", "_env_vars")):
        return False
    if normalized.endswith("_path") and any(
        marker in normalized for marker in ("private_key", "signing_key", "certificate", "credential")
    ):
        return True
    return _is_sensitive_normalized_key(normalized)


def _is_safe_reference_value(normalized_key: str, value: Any) -> bool:
    if normalized_key.endswith(("_env", "_env_var")):
        return isinstance(value, str) and bool(_ENVIRONMENT_NAME.fullmatch(value.strip()))
    if normalized_key in {"credential_env_vars", "required_credentials"} or normalized_key.endswith("_env_vars"):
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and all(
            isinstance(item, str) and bool(_ENVIRONMENT_NAME.fullmatch(item.strip())) for item in value
        )
    if normalized_key.endswith("_path"):
        if value in (None, ""):
            return True
        if not isinstance(value, str):
            return False
        text = value.strip()
        return (
            bool(text)
            and "\x00" not in text
            and "\n" not in text
            and "\r" not in text
            and "-----BEGIN" not in text
            and not _looks_like_embedded_secret(text)
        )
    return False


def _looks_like_embedded_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    upper = text.upper()
    if "-----BEGIN" in upper and "PRIVATE KEY-----" in upper:
        return True
    if _JWT_VALUE.fullmatch(text):
        return True
    if "://" in text:
        try:
            parsed = urlsplit(text)
        except ValueError:
            return False
        return parsed.username is not None or parsed.password is not None
    return False


def _has_material_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value)
    return True


def iter_persisted_secret_paths(value: Any, path: Tuple[str, ...] = ()) -> Iterator[Tuple[str, ...]]:
    """Yield key paths that would persist non-empty credential values."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = (*path, key)
            normalized = normalize_config_key(key)
            if normalized in _SENSITIVE_CONTAINER_KEYS and _has_material_value(child):
                yield child_path
                continue
            if _is_reference_key(normalized):
                if not _is_safe_reference_value(normalized, child):
                    yield child_path
                continue
            if is_sensitive_config_key(key) and _has_material_value(child):
                yield child_path
                continue
            if _looks_like_embedded_secret(child):
                yield child_path
                continue
            yield from iter_persisted_secret_paths(child, child_path)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            child_path = (*path, str(index))
            if _looks_like_embedded_secret(child):
                yield child_path
                continue
            yield from iter_persisted_secret_paths(child, child_path)


def assert_no_persisted_secrets(value: Any) -> None:
    """Reject configuration containing credential values without echoing them."""

    paths = list(iter_persisted_secret_paths(value))
    if not paths:
        return
    labels = [".".join(path) for path in paths[:8]]
    suffix = "" if len(paths) <= 8 else f" (and {len(paths) - 8} more)"
    raise ConfigSecurityError(
        "Credential values must be supplied through environment variables and cannot be persisted in configuration: "
        + ", ".join(labels)
        + suffix
    )
