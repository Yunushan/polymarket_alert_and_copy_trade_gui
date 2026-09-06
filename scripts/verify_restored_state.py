"""Exercise a restored state tree without credentials, mutations, or venue access."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import math
import os
import secrets
import socket
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_FILENAMES = {
    "POLYMARKET_ANALYTICS_CACHE_PATH": "polymarket_analytics_cache.json",
    "POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": "polymarket_live_validation_reports.json",
    "POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH": "polymarket_live_validation_decisions.json",
    "POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH": (
        "polymarket_live_validation_promotion_proposal_snapshots.json"
    ),
}
APPLICATION_CHECK_FIELDS = {
    "schema_version", "config_loaded", "health_ready", "state_readable",
    "mutations_blocked", "outbound_attempts", "files_unchanged",
    "sqlite_databases_checked", "api_version", "runtime_source_revision",
    "runtime_frontend_sha256",
}


def application_check_valid(
    value: Any, *, version: str, revision: str, frontend_sha256: str,
) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == APPLICATION_CHECK_FIELDS
        and type(value["schema_version"]) is int and value["schema_version"] == 1
        and all(value[name] is True for name in (
            "config_loaded", "health_ready", "state_readable", "mutations_blocked", "files_unchanged",
        ))
        and type(value["outbound_attempts"]) is int and value["outbound_attempts"] == 0
        and type(value["sqlite_databases_checked"]) is int and value["sqlite_databases_checked"] >= 0
        and value["api_version"] == version
        and value["runtime_source_revision"] == revision
        and value["runtime_frontend_sha256"] == frontend_sha256
    )


def isolated_environment(state: Path) -> dict[str, str]:
    # Do not inherit API keys, proxies, Python import hooks, or durable-store paths.
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    environment.update({name: str(state / filename) for name, filename in STATE_FILENAMES.items()})
    environment.update({
        "PREDICTION_MARKET_CONFIG_PATH": str(state / "config.json"),
        "HOME": str(state.parent), "USERPROFILE": str(state.parent),
        "TMP": str(state.parent), "TEMP": str(state.parent), "TMPDIR": str(state.parent),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
    })
    return environment


def _inventory(state: Path) -> dict[str, str]:
    inventory = {}
    for path in state.rglob("*"):
        if path.is_symlink():
            raise ValueError("Restored state contains a symbolic link")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            inventory[path.relative_to(state).as_posix()] = digest.hexdigest()
    return inventory


def _strict_json(path: Path, maximum_bytes: int) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Restored JSON contains duplicate keys")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("Restored JSON contains a non-finite number")

    def number(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Restored JSON contains a non-finite number")
        return parsed

    with path.open("rb") as stream:
        raw = stream.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError("Restored JSON exceeds the application readiness size limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def _check_sqlite(state: Path) -> int:
    count = 0
    for path in state.rglob("*"):
        if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"} or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\0":
                raise ValueError("Restored SQLite header is missing or corrupt")
        # These are isolated snapshots, not live WAL databases. Immutable mode
        # prevents even journal/sidecar creation during structural validation.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Restored SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Restored SQLite foreign-key check failed")
        finally:
            connection.close()
        count += 1
    return count


def probe_restored_application(state: Path, frontend_dir: Path) -> dict[str, Any]:
    """Run only in the bounded, environment-isolated child created by the collector."""
    from core.storage import load_config
    from web_api import MAX_READINESS_JSON_STORE_BYTES, ReactGuiHandler, ReactGuiServer

    before = _inventory(state)
    if "config.json" not in before:
        raise ValueError("Restore drill requires the backed-up configuration, not empty defaults")
    for filename in ("config.json", *STATE_FILENAMES.values()):
        target = state / filename
        if target.exists():
            parsed = _strict_json(target, MAX_READINESS_JSON_STORE_BYTES)
            if not isinstance(parsed, dict):
                raise ValueError("Restored application store must contain a JSON object")
    load_config(state / "config.json")
    databases = _check_sqlite(state)
    attempted_connections: list[None] = []
    probe_thread = threading.get_ident()
    allowed_address: tuple[str, int] | None = None
    original_connect = socket.socket.connect

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        attempted_connections.append(None)
        raise PermissionError("Restore drill forbids backend outbound connections")

    def connect(sock: socket.socket, address: Any) -> Any:
        if threading.get_ident() != probe_thread or allowed_address is None or address != allowed_address:
            return deny()
        return original_connect(sock, address)

    class ReadOnlyHandler(ReactGuiHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path not in {"/api/health", "/api/state"}:
                self.send_error(405)
                return
            super().do_GET()

        def reject_mutation(self) -> None:
            self.send_error(405)

        do_POST = do_PUT = do_PATCH = do_DELETE = reject_mutation

    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", deny),
        patch.object(socket, "getaddrinfo", deny),
        patch.object(socket, "gethostbyname", deny),
        patch.object(socket, "gethostbyname_ex", deny),
        patch.object(socket, "getfqdn", return_value="localhost"),
    ):
        token = secrets.token_urlsafe(32)
        server = ReactGuiServer(
            ("127.0.0.1", 0), ReadOnlyHandler, config_path=state / "config.json",
            frontend_dir=frontend_dir, api_token=token,
        )
        allowed_address = ("127.0.0.1", server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method: str, path: str) -> tuple[int, bytes]:
            connection = http.client.HTTPConnection(*allowed_address, timeout=5)
            try:
                connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                connection.sock.settimeout(5)
                connection.sock.connect(allowed_address)
                connection.request(method, path, headers={"Authorization": f"Bearer {token}"})
                response = connection.getresponse()
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("Restored application response exceeds probe limits")
                return response.status, body
            finally:
                connection.close()

        try:
            status, body = request("GET", "/api/health")
            health = json.loads(body)
            if status != 200 or health.get("readiness", {}).get("ready") is not True:
                raise ValueError("Restored application is not ready")
            status, body = request("GET", "/api/state")
            if status != 200 or not isinstance(json.loads(body), dict):
                raise ValueError("Restored application state cannot be served")
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                if request(method, "/api/config")[0] != 405:
                    raise ValueError("Restore drill admitted a mutation request")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("Restored application did not stop")
    if attempted_connections:
        raise ValueError("Restored application attempted outbound connections")
    if _inventory(state) != before:
        raise ValueError("Restored application changed the backup contents")
    return {
        "schema_version": 1, "config_loaded": True, "health_ready": True,
        "state_readable": True, "mutations_blocked": True, "outbound_attempts": 0,
        "files_unchanged": True, "sqlite_databases_checked": databases,
        "api_version": health["api_version"],
        "runtime_source_revision": health["runtime_source_revision"],
        "runtime_frontend_sha256": health["runtime_frontend_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--frontend-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        state = args.state.resolve(strict=True)
        environment = isolated_environment(state)
        os.environ.clear()
        os.environ.update(environment)
        with contextlib.redirect_stdout(sys.stderr):
            result = probe_restored_application(state, args.frontend_dir.resolve(strict=True))
    except Exception as exc:
        # Never copy configuration content, HTTP responses, or credentials to evidence logs.
        print(json.dumps({"error": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
