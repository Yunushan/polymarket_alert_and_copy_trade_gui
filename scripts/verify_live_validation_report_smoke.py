from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websocket


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_api import ReactGuiHandler, ReactGuiServer  # noqa: E402


DEFAULT_FRONTEND_DIR = ROOT / "frontend" / "dist"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_RENDER_TIMEOUT_SECONDS = 60
DEFAULT_SERVER_READY_TIMEOUT_SECONDS = 45
SEED_SECRET = "browser-smoke-secret"

SEEDED_REPORT: Dict[str, Any] = {
    "generated_at": 123.0,
    "market_id": "polymarket",
    "mode": "browser_smoke_seed",
    "selected": True,
    "enabled": True,
    "api_key": SEED_SECRET,
    "stage_gates": {
        "public_live_checks": "passed",
        "credential_readiness": "blocked",
        "credentialed_read_checks": "blocked",
        "bridge_address_checks": "blocked",
        "funded_live_order_check": "blocked",
        "credentialed_read_ok": False,
        "safe_to_attempt_funded_order": False,
        "requires_explicit_live_approval": True,
        "next_step": "browser smoke uses seeded local reports only",
    },
    "funded_execution_exposed": False,
    "notes": ["Seeded browser-smoke report; no credentials or funded actions are used."],
}

INVALID_REPORT: Dict[str, Any] = {
    "generated_at": 123.0,
    "stage_gates": {
        "credentialed_read_ok": True,
        "safe_to_attempt_funded_order": False,
    },
}

REQUIRED_DOM_FRAGMENTS = (
    "Polymarket Live Validation",
    "Validation Reports",
    "Schema Diagnostics",
    "Accepted modes",
    "schema: accepted",
    "Payload hash",
    "Allow duplicate import",
    "Review",
    "Promotion Decision Ledger",
    "Promotion Proposal Preview",
    "Refresh Proposal",
    "Save Snapshot",
    "Proposal Snapshot Archive",
    "Store Snapshot",
    "Refresh Reports",
    "CLI JSON report import",
    "browser smoke",
    "/api/polymarket/live-validation/reports/",
    "/export.json",
)


class BrowserStartupError(RuntimeError):
    pass


class BrowserRenderError(RuntimeError):
    pass


class SmokeReactGuiHandler(ReactGuiHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


class SmokeReactGuiServer(ReactGuiServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        return


def close_browser_debug_endpoint(port: int | None, browser_ws_path: str) -> None:
    if port is None or not browser_ws_path:
        return
    ws = None
    try:
        ws = websocket.create_connection(f"ws://127.0.0.1:{port}{browser_ws_path}", timeout=2)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
    except Exception:
        pass
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    idempotency_key: str = "",
) -> Tuple[int, Dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


def request_raw(base_url: str, path: str, *, method: str = "GET") -> Tuple[int, bytes]:
    request = Request(f"{base_url}{path}", method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def wait_for_state_api(
    base_url: str,
    *,
    timeout_seconds: int = DEFAULT_SERVER_READY_TIMEOUT_SECONDS,
    retry_interval_seconds: float = 0.25,
) -> Dict[str, Any]:
    """Wait for the temporary server to finish its first state response.

    The server socket is available before the first request has completed its
    config and report-store initialization. A transient accept/read timeout is
    therefore a startup condition, not evidence that the smoke route failed.
    """

    deadline = time.monotonic() + max(1, int(timeout_seconds))
    last_error = "no response"
    while True:
        try:
            status, state = request_json(base_url, "/api/state")
            if status == 200 and isinstance(state, dict):
                return state
            last_error = f"HTTP {status}: {state}"
        except (OSError, TimeoutError, URLError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise SystemExit(
                "Live Safety browser smoke state API did not become ready within "
                f"{max(1, int(timeout_seconds))} seconds: {last_error}"
            )
        time.sleep(max(0.0, retry_interval_seconds))


def find_browser(explicit_path: str = "") -> str:
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    for env_key in ("PREDICTION_MARKET_BROWSER_PATH", "EDGE_PATH", "CHROME_PATH"):
        value = os.environ.get(env_key)
        if value:
            candidates.append(value)
    for executable in ("msedge", "chrome", "chromium", "google-chrome", "google-chrome-stable"):
        resolved = shutil.which(executable)
        if resolved:
            candidates.append(resolved)
    if sys.platform == "win32":
        candidates.extend(
            [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                str(Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe"),
            ]
        )
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return str(path)
    raise SystemExit(
        "No Chromium/Edge browser executable was found. Set PREDICTION_MARKET_BROWSER_PATH to run the live validation browser smoke."
    )


def _browser_dom_check_once(
    browser_path: str,
    url: str,
    profile_dir: Path,
    *,
    headless_arg: str,
    timeout_seconds: int,
) -> Dict[str, Any]:
    command = [
        browser_path,
        headless_arg,
        "--disable-background-networking",
        "--disable-component-extensions-with-background-pages",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-sync",
        "--no-default-browser-check",
        "--no-first-run",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--window-size=1280,900",
        f"--user-data-dir={profile_dir}",
        url,
    ]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser_port: int | None = None
    browser_ws_path = ""
    try:
        deadline = time.monotonic() + max(5, int(timeout_seconds))
        port_file = profile_dir / "DevToolsActivePort"
        while time.monotonic() < deadline and not port_file.exists():
            time.sleep(0.1)
        if not port_file.exists():
            exit_code = process.poll()
            if exit_code is None:
                raise BrowserStartupError(f"{headless_arg} did not expose a debugging port.")
            raise BrowserStartupError(f"{headless_arg} exited before exposing DevTools with code {exit_code}.")

        port_lines = port_file.read_text(encoding="utf-8").splitlines()
        port = int(port_lines[0])
        browser_port = port
        browser_ws_path = port_lines[1] if len(port_lines) > 1 else ""
        page_ws_url = ""
        while time.monotonic() < deadline and not page_ws_url:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    tabs = json.loads(response.read().decode("utf-8"))
            except Exception:
                tabs = []
            for item in tabs:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "page"
                    and item.get("webSocketDebuggerUrl")
                    and str(item.get("url") or "").startswith(url)
                ):
                    page_ws_url = str(item["webSocketDebuggerUrl"])
                    break
            if not page_ws_url:
                time.sleep(0.1)
        if not page_ws_url:
            raise BrowserStartupError(f"{headless_arg} did not expose a page debugging target.")

        ws = websocket.create_connection(page_ws_url, timeout=5)
        try:
            command_id = 0

            def cdp(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
                nonlocal command_id
                command_id += 1
                ws.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(ws.recv())
                    if message.get("id") == command_id:
                        return message

            cdp("Page.enable")
            cdp("Runtime.enable")
            expression = f"""
(() => {{
  const fragments = {json.dumps(list(REQUIRED_DOM_FRAGMENTS))};
  const html = document.documentElement ? document.documentElement.outerHTML : "";
  const text = document.body ? document.body.innerText : "";
  const haystack = html + "\\n" + text;
  return {{
    title: document.title,
    url: location.href,
    htmlLength: html.length,
    textLength: text.length,
    missing: fragments.filter((fragment) => !haystack.includes(fragment)),
    secretPresent: haystack.includes({json.dumps(SEED_SECRET)}),
    textSnippet: text.slice(0, 500)
  }};
}})()
"""
            last_value: Dict[str, Any] = {}
            reload_attempts = 0
            next_reload_at = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                value = response.get("result", {}).get("result", {}).get("value")
                if isinstance(value, dict):
                    last_value = value
                    if value.get("secretPresent"):
                        raise SystemExit("Live Safety browser smoke DOM leaked the seeded secret.")
                    if not value.get("missing"):
                        return value
                    transient_shell = "API loading" in str(value.get("textSnippet") or "") or "Failed to fetch" in str(value.get("textSnippet") or "")
                    if (
                        reload_attempts < 3
                        and time.monotonic() >= next_reload_at
                        and (
                            (int(value.get("htmlLength") or 0) < 1000 and int(value.get("textLength") or 0) == 0)
                            or transient_shell
                        )
                    ):
                        reload_attempts += 1
                        next_reload_at = time.monotonic() + 10
                        cdp("Page.reload", {"ignoreCache": True})
                time.sleep(0.25)
            missing = last_value.get("missing") if isinstance(last_value, dict) else []
            detail = json.dumps(last_value, sort_keys=True) if last_value else "{}"
            raise BrowserRenderError("Live Safety browser smoke DOM is missing: " + ", ".join(str(item) for item in missing) + f"; last={detail}")
        finally:
            try:
                ws.close()
            except Exception:
                pass
    finally:
        close_browser_debug_endpoint(browser_port, browser_ws_path)
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def browser_dom_check(browser_path: str, url: str, profile_dir: Path, *, timeout_seconds: int) -> Dict[str, Any]:
    errors = []
    for headless_arg, suffix in (("--headless=new", ""), ("--headless", "-legacy")):
        attempt_profile_dir = profile_dir if not suffix else profile_dir.with_name(f"{profile_dir.name}{suffix}")
        try:
            return _browser_dom_check_once(
                browser_path,
                url,
                attempt_profile_dir,
                headless_arg=headless_arg,
                timeout_seconds=timeout_seconds,
            )
        except (BrowserStartupError, BrowserRenderError) as exc:
            errors.append(str(exc))
    raise SystemExit("Headless browser failed to start for DOM smoke: " + " ".join(errors))


def start_server(host: str, port: int, config_path: Path, frontend_dir: Path) -> Tuple[ReactGuiServer, threading.Thread, str]:
    server = SmokeReactGuiServer((host, port), SmokeReactGuiHandler, config_path=config_path, frontend_dir=frontend_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://{host}:{server.server_address[1]}"


def run_smoke(args: argparse.Namespace) -> Dict[str, Any]:
    frontend_dir = args.frontend_dir.resolve()
    if not (frontend_dir / "index.html").exists():
        raise SystemExit(f"React build is missing at {frontend_dir}; run npm run build or python verify.py --frontend-build first.")

    browser_path = find_browser(args.browser_path)
    with tempfile.TemporaryDirectory(prefix="polymarket-live-smoke-", ignore_cleanup_errors=True) as tmpdir:
        temp_root = Path(tmpdir)
        config_path = temp_root / "config.json"
        report_path = temp_root / "live-validation-reports.json"
        decision_path = temp_root / "live-validation-decisions.json"
        snapshot_path = temp_root / "live-validation-proposal-snapshots.json"
        old_report_path = os.environ.get("POLYMARKET_LIVE_VALIDATION_REPORTS_PATH")
        old_decision_path = os.environ.get("POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH")
        old_snapshot_path = os.environ.get("POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH")
        os.environ["POLYMARKET_LIVE_VALIDATION_REPORTS_PATH"] = str(report_path)
        os.environ["POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH"] = str(decision_path)
        os.environ["POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH"] = str(snapshot_path)
        server: ReactGuiServer | None = None
        thread: threading.Thread | None = None
        try:
            server, thread, base_url = start_server(args.host, args.port, config_path, frontend_dir)
            wait_for_state_api(base_url)
            status, stored = request_json(
                base_url,
                "/api/polymarket/live-validation/reports",
                method="POST",
                idempotency_key="browser-smoke-report-seed-v1",
                payload={
                    "label": "browser smoke",
                    "source": "browser_smoke",
                    "report_json": json.dumps(SEEDED_REPORT),
                },
            )
            if status != 200:
                raise SystemExit(f"Seeding live validation report failed with HTTP {status}: {stored}")
            report_key = str(stored.get("stored", {}).get("key") or "")
            if not report_key:
                raise SystemExit("Seeding live validation report did not return a report key.")

            status, opened = request_json(base_url, f"/api/polymarket/live-validation/reports/{report_key}")
            if status != 200:
                raise SystemExit(f"Opening live validation report failed with HTTP {status}: {opened}")
            opened_text = json.dumps(opened, sort_keys=True)
            if SEED_SECRET in opened_text:
                raise SystemExit("Opened live validation report leaked the seeded secret.")
            if opened.get("entry", {}).get("payload", {}).get("api_key") != "***":
                raise SystemExit("Opened live validation report did not redact the seeded API key.")
            if not opened.get("entry", {}).get("schema_validation", {}).get("ok"):
                raise SystemExit("Opened live validation report did not include accepted schema validation metadata.")
            if not opened.get("entry", {}).get("payload_hash"):
                raise SystemExit("Opened live validation report did not include a redacted payload hash.")

            status, export_body = request_raw(base_url, f"/api/polymarket/live-validation/reports/{report_key}/export.json")
            export_text = export_body.decode("utf-8", errors="replace")
            if status != 200:
                raise SystemExit(f"Exporting live validation report failed with HTTP {status}: {export_text}")
            if SEED_SECRET in export_text:
                raise SystemExit("Exported live validation report leaked the seeded secret.")
            exported = json.loads(export_text)
            if not exported.get("entry", {}).get("schema_validation", {}).get("ok"):
                raise SystemExit("Exported live validation report did not preserve schema validation metadata.")
            if exported.get("entry", {}).get("payload_hash") != opened.get("entry", {}).get("payload_hash"):
                raise SystemExit("Exported live validation report did not preserve payload-hash metadata.")

            status, review_body = request_raw(base_url, f"/api/polymarket/live-validation/reports/{report_key}/review.json")
            review_text = review_body.decode("utf-8", errors="replace")
            if status != 200:
                raise SystemExit(f"Exporting live validation review JSON failed with HTTP {status}: {review_text}")
            if SEED_SECRET in review_text:
                raise SystemExit("Live validation review JSON leaked the seeded secret.")
            review = json.loads(review_text)
            if review.get("bundle", {}).get("static_coverage_mutated") is not False:
                raise SystemExit("Live validation review JSON did not preserve the no-static-coverage-mutation guard.")

            status, decision = request_json(
                base_url,
                "/api/polymarket/live-validation/decisions",
                method="POST",
                idempotency_key="browser-smoke-decision-seed-v1",
                payload={
                    "report_key": report_key,
                    "payload_hash": review.get("bundle", {}).get("report", {}).get("payload_hash"),
                    "target_tier": "credential_live_verified",
                    "decision": "rejected",
                    "reviewer": "browser-smoke",
                    "reviewer_note": "Seeded browser smoke reports cannot promote credential live verification.",
                    "review_bundle_hash": review.get("bundle", {}).get("review_bundle_hash"),
                },
            )
            if status != 200:
                raise SystemExit(f"Recording live validation decision failed with HTTP {status}: {decision}")
            if decision.get("stored", {}).get("static_coverage_mutated") is not False:
                raise SystemExit("Live validation decision mutated static coverage.")

            status, ledger_body = request_raw(base_url, "/api/polymarket/live-validation/decisions/export.md")
            ledger_text = ledger_body.decode("utf-8", errors="replace")
            if status != 200 or "Promotion Decision Ledger" not in ledger_text:
                raise SystemExit(f"Exporting live validation decision ledger failed with HTTP {status}: {ledger_text}")
            if SEED_SECRET in ledger_text:
                raise SystemExit("Live validation decision ledger leaked the seeded secret.")

            status, proposal_body = request_raw(base_url, "/api/polymarket/live-validation/promotion-proposal/export.md")
            proposal_text = proposal_body.decode("utf-8", errors="replace")
            if status != 200 or "Coverage Promotion Proposal" not in proposal_text:
                raise SystemExit(f"Exporting live validation promotion proposal failed with HTTP {status}: {proposal_text}")
            if "Automerge enabled: false" not in proposal_text:
                raise SystemExit("Live validation promotion proposal did not include the automerge guard.")
            if SEED_SECRET in proposal_text:
                raise SystemExit("Live validation promotion proposal leaked the seeded secret.")

            status, snapshots = request_json(
                base_url,
                "/api/polymarket/live-validation/promotion-proposal/snapshots",
                method="POST",
                idempotency_key="browser-smoke-proposal-snapshot-seed-v1",
                payload={"target_tier": "credential_live_verified", "source": "browser_smoke"},
            )
            if status != 200 or snapshots.get("counts", {}).get("entries") != 1:
                raise SystemExit(f"Storing live validation promotion proposal snapshot failed with HTTP {status}: {snapshots}")
            snapshot_key = snapshots.get("stored", {}).get("key")
            if not snapshot_key:
                raise SystemExit("Stored live validation promotion proposal snapshot did not return a key.")

            status, snapshot = request_json(
                base_url,
                f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}",
            )
            if status != 200 or snapshot.get("entry", {}).get("snapshot_status") not in {"current", "stale"}:
                raise SystemExit(f"Opening live validation promotion proposal snapshot failed with HTTP {status}: {snapshot}")
            if snapshot.get("entry", {}).get("static_coverage_mutated") is not False:
                raise SystemExit("Live validation promotion proposal snapshot mutated static coverage.")
            if SEED_SECRET in json.dumps(snapshot, sort_keys=True):
                raise SystemExit("Live validation promotion proposal snapshot leaked the seeded secret.")

            status, snapshot_markdown_body = request_raw(
                base_url,
                f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/export.md",
            )
            snapshot_markdown = snapshot_markdown_body.decode("utf-8", errors="replace")
            if status != 200 or "Promotion Proposal Snapshot" not in snapshot_markdown:
                raise SystemExit(f"Exporting live validation promotion proposal snapshot failed with HTTP {status}: {snapshot_markdown}")
            if SEED_SECRET in snapshot_markdown:
                raise SystemExit("Live validation promotion proposal snapshot Markdown leaked the seeded secret.")

            status, snapshot_diff_body = request_raw(
                base_url,
                f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/diff.json",
            )
            snapshot_diff = json.loads(snapshot_diff_body.decode("utf-8", errors="replace"))
            if status != 200 or snapshot_diff.get("snapshot_key") != snapshot_key:
                raise SystemExit(f"Exporting live validation promotion proposal snapshot diff failed with HTTP {status}: {snapshot_diff}")
            if snapshot_diff.get("static_coverage_mutated") is not False or SEED_SECRET in json.dumps(snapshot_diff, sort_keys=True):
                raise SystemExit("Live validation promotion proposal snapshot diff was unsafe or leaked the seeded secret.")

            status, snapshot_diff_markdown_body = request_raw(
                base_url,
                f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/diff.md",
            )
            snapshot_diff_markdown = snapshot_diff_markdown_body.decode("utf-8", errors="replace")
            if status != 200 or "Current-vs-Snapshot Diff" not in snapshot_diff_markdown:
                raise SystemExit(
                    f"Exporting live validation promotion proposal snapshot diff Markdown failed with HTTP {status}: "
                    f"{snapshot_diff_markdown}"
                )
            if SEED_SECRET in snapshot_diff_markdown:
                raise SystemExit("Live validation promotion proposal snapshot diff Markdown leaked the seeded secret.")

            status, deleted_snapshot = request_raw(
                base_url,
                f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}",
                method="DELETE",
            )
            if status != 200:
                raise SystemExit(
                    f"Deleting live validation promotion proposal snapshot failed with HTTP {status}: "
                    f"{deleted_snapshot.decode('utf-8', errors='replace')}"
                )

            status, markdown_body = request_raw(base_url, f"/api/polymarket/live-validation/reports/{report_key}/review.md")
            markdown_text = markdown_body.decode("utf-8", errors="replace")
            if status != 200:
                raise SystemExit(f"Exporting live validation review Markdown failed with HTTP {status}: {markdown_text}")
            if SEED_SECRET in markdown_text:
                raise SystemExit("Live validation review Markdown leaked the seeded secret.")
            if "Static coverage mutated: false" not in markdown_text:
                raise SystemExit("Live validation review Markdown did not include the no-static-coverage-mutation guard.")

            status, duplicate = request_json(
                base_url,
                "/api/polymarket/live-validation/reports",
                method="POST",
                idempotency_key="browser-smoke-report-duplicate-v1",
                payload={
                    "label": "browser smoke duplicate",
                    "source": "browser_smoke",
                    "report_json": json.dumps(SEEDED_REPORT),
                },
            )
            if status != 200:
                raise SystemExit(f"Duplicate live validation report import failed with HTTP {status}: {duplicate}")
            if duplicate.get("stored", {}).get("stored") is not False or not duplicate.get("stored", {}).get("duplicate"):
                raise SystemExit(f"Duplicate live validation report was not skipped by default: {duplicate}")
            if duplicate.get("counts", {}).get("entries") != 1:
                raise SystemExit("Duplicate live validation report changed stored report count: " + json.dumps(duplicate, sort_keys=True))

            status, rejected = request_json(
                base_url,
                "/api/polymarket/live-validation/reports",
                method="POST",
                idempotency_key="browser-smoke-report-invalid-v1",
                payload={
                    "label": "invalid browser smoke",
                    "source": "browser_smoke_invalid",
                    "report_json": json.dumps(INVALID_REPORT),
                },
            )
            if status != 400:
                raise SystemExit(f"Invalid live validation report import should fail with HTTP 400, got {status}: {rejected}")
            validation = rejected.get("error", {}).get("details", {}).get("schema_validation", {})
            if validation.get("ok") is not False or "strict_cli" not in validation.get("accepted_modes", []):
                raise SystemExit(f"Invalid live validation report import did not return structured schema details: {rejected}")
            status, listing_after_reject = request_json(base_url, "/api/polymarket/live-validation/reports")
            if status != 200 or listing_after_reject.get("counts", {}).get("entries") != 1:
                raise SystemExit(
                    "Invalid live validation report import changed stored report count: "
                    + json.dumps(listing_after_reject, sort_keys=True)
                )

            profile_dir = temp_root / "edge-profile"
            dom_check = browser_dom_check(
                browser_path,
                f"{base_url}/?tab=live",
                profile_dir,
                timeout_seconds=args.render_timeout_seconds,
            )

            return {
                "ok": True,
                "base_url": base_url,
                "browser": browser_path,
                "report_key": report_key,
                "report_path": str(report_path),
                "config_path": str(config_path),
                "dom_fragments_checked": list(REQUIRED_DOM_FRAGMENTS),
                "dom_check": dom_check,
                "invalid_import_schema_error": validation,
                "duplicate_import": duplicate.get("stored", {}),
                "review_bundle": {
                    "json_source": review.get("bundle", {}).get("source"),
                    "markdown_checked": True,
                },
                "decision_ledger": decision.get("stored", {}),
                "funded_execution_exposed": False,
            }
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=5)
            if old_report_path is None:
                os.environ.pop("POLYMARKET_LIVE_VALIDATION_REPORTS_PATH", None)
            else:
                os.environ["POLYMARKET_LIVE_VALIDATION_REPORTS_PATH"] = old_report_path
            if old_decision_path is None:
                os.environ.pop("POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH", None)
            else:
                os.environ["POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH"] = old_decision_path
            if old_snapshot_path is None:
                os.environ.pop("POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH", None)
            else:
                os.environ["POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH"] = old_snapshot_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the built Live Safety report-history UI with seeded local reports.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--render-timeout-seconds", type=int, default=DEFAULT_RENDER_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Print the smoke payload as JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_smoke(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[ok] Live Safety report-history browser smoke ({result['report_key']})")


if __name__ == "__main__":
    main()
