"""Run browser acceptance against isolated local state, with no venue egress."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_api import ReactGuiHandler, ReactGuiServer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=shutil.which("node"))
    args, playwright_args = parser.parse_known_args(argv)
    frontend = ROOT / "frontend"
    cli = frontend / "node_modules" / "@playwright" / "test" / "cli.js"
    if not args.node or not cli.is_file() or not (frontend / "dist" / "index.html").is_file():
        parser.error("Node, frontend npm ci and npm run build are required before browser acceptance.")

    # No outbound Python socket is needed: only the browser connects to this server.
    forbidden_connections: list[str] = []

    def deny_connection(*_args, **_kwargs):
        forbidden_connections.append("outbound socket")
        raise PermissionError("Browser acceptance forbids backend outbound connections.")

    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marketsentinel-browser-", dir=cache) as temporary:
        state = Path(temporary)
        # Keep reloads on one build even if a developer rebuilds the workspace.
        frontend_snapshot = state / "frontend"
        shutil.copytree(frontend / "dist", frontend_snapshot)
        environment = {
            "POLYMARKET_ANALYTICS_CACHE_PATH": str(state / "analytics.json"),
            "POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(state / "reports.json"),
            "POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH": str(state / "decisions.json"),
            "POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH": str(state / "snapshots.json"),
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(socket.socket, "connect", deny_connection),
            patch.object(socket.socket, "connect_ex", deny_connection),
        ):
            server = ReactGuiServer(
                ("127.0.0.1", 0), ReactGuiHandler,
                config_path=state / "config.json", frontend_dir=frontend_snapshot,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    [args.node, str(cli), "test", *playwright_args],
                    cwd=frontend,
                    env={
                        **os.environ,
                        "MARKET_SENTINEL_BROWSER_TEST_ORIGIN": f"http://127.0.0.1:{server.server_address[1]}",
                    },
                    timeout=600,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Browser acceptance backend did not stop.")
            if forbidden_connections:
                raise RuntimeError(f"Browser acceptance attempted {len(forbidden_connections)} outbound connections.")
            return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
