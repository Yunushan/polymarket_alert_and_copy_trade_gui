from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.request import urlopen

import websocket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_live_validation_report_smoke import (  # noqa: E402
    BrowserStartupError,
    close_browser_debug_endpoint,
    find_browser,
    start_server,
)
from web_api import ReactGuiServer  # noqa: E402


DEFAULT_FRONTEND_DIR = ROOT / "frontend" / "dist"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_BROWSER_STARTUP_TIMEOUT_SECONDS = 60
DEFAULT_RENDER_TIMEOUT_SECONDS = 60
DEFAULT_CDP_COMMAND_TIMEOUT_SECONDS = 15
DEFAULT_BROWSER_ATTEMPTS_PER_MODE = 2
CDP_RECV_POLL_SECONDS = 2.0

REQUIRED_TEXT = (
    "MarketSentinel",
    "Markets",
    "Analytics",
    "Live Safety",
    "Wallets",
    "Paper",
    "Settings",
)

MOBILE_TARGETS: Dict[str, Dict[str, Any]] = {
    "android-14": {
        "platform": "Android",
        "os_version": "14",
        "api_level": 34,
        "width": 412,
        "height": 915,
        "device_scale_factor": 2.625,
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
        ),
    },
    "android-15": {
        "platform": "Android",
        "os_version": "15",
        "api_level": 35,
        "width": 412,
        "height": 915,
        "device_scale_factor": 2.625,
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
    },
    "android-16": {
        "platform": "Android",
        "os_version": "16",
        "api_level": 36,
        "width": 412,
        "height": 915,
        "device_scale_factor": 2.625,
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36"
        ),
    },
    "ios-15": {
        "platform": "iOS",
        "os_version": "15",
        "width": 390,
        "height": 844,
        "device_scale_factor": 3,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
        ),
    },
    "ios-16": {
        "platform": "iOS",
        "os_version": "16",
        "width": 390,
        "height": 844,
        "device_scale_factor": 3,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        ),
    },
    "ios-18": {
        "platform": "iOS",
        "os_version": "18",
        "width": 393,
        "height": 852,
        "device_scale_factor": 3,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    },
    "ios-26": {
        "platform": "iOS",
        "os_version": "26",
        "width": 393,
        "height": 852,
        "device_scale_factor": 3,
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1"
        ),
    },
}


class BrowserRenderTimeoutError(RuntimeError):
    """Raised when a browser starts but the mobile UI does not render in time."""


def _target_names(target_arg: str) -> Iterable[str]:
    if target_arg == "all":
        return MOBILE_TARGETS.keys()
    names = [item.strip() for item in target_arg.split(",") if item.strip()]
    unknown = [item for item in names if item not in MOBILE_TARGETS]
    if unknown:
        raise SystemExit("Unknown mobile smoke target(s): " + ", ".join(unknown))
    return names


def _cdp_call(
    ws: Any,
    *,
    command_id: int,
    method: str,
    params: Dict[str, Any] | None,
    overall_deadline: float,
    target_name: str,
    headless_arg: str,
) -> Dict[str, Any]:
    response_deadline = min(overall_deadline, time.monotonic() + DEFAULT_CDP_COMMAND_TIMEOUT_SECONDS)
    payload = {"id": command_id, "method": method, "params": params or {}}
    try:
        ws.send(json.dumps(payload))
    except (OSError, websocket.WebSocketConnectionClosedException) as exc:
        raise BrowserStartupError(
            f"{target_name} {headless_arg} lost CDP connection while sending {method}: {exc}"
        ) from exc

    while True:
        remaining = response_deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserStartupError(
                f"{target_name} {headless_arg} timed out waiting for CDP response to {method}."
            )
        try:
            ws.settimeout(max(0.25, min(CDP_RECV_POLL_SECONDS, remaining)))
            message = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException as exc:
            if time.monotonic() >= response_deadline:
                raise BrowserStartupError(
                    f"{target_name} {headless_arg} timed out waiting for CDP response to {method}."
                ) from exc
            continue
        except (OSError, websocket.WebSocketConnectionClosedException) as exc:
            raise BrowserStartupError(f"{target_name} {headless_arg} lost CDP connection during {method}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BrowserStartupError(f"{target_name} {headless_arg} returned invalid CDP JSON during {method}.") from exc
        if message.get("id") == command_id:
            return message


def _browser_mobile_check_once(
    browser_path: str,
    url: str,
    profile_dir: Path,
    target_name: str,
    target: Dict[str, Any],
    *,
    headless_arg: str,
    startup_timeout_seconds: int,
    render_timeout_seconds: int,
) -> Dict[str, Any]:
    width = int(target["width"])
    height = int(target["height"])
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
        f"--window-size={width},{height}",
        f"--user-agent={target['user_agent']}",
        f"--user-data-dir={profile_dir}",
        "about:blank",
    ]
    import subprocess

    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser_port: int | None = None
    browser_ws_path = ""
    try:
        startup_deadline = time.monotonic() + max(5, int(startup_timeout_seconds))
        port_file = profile_dir / "DevToolsActivePort"
        while time.monotonic() < startup_deadline and not port_file.exists():
            time.sleep(0.1)
        if not port_file.exists():
            exit_code = process.poll()
            if exit_code is None:
                raise BrowserStartupError(f"{target_name} {headless_arg} did not expose a debugging port.")
            raise BrowserStartupError(f"{target_name} {headless_arg} exited before exposing DevTools with code {exit_code}.")

        port_lines = port_file.read_text(encoding="utf-8").splitlines()
        port = int(port_lines[0])
        browser_port = port
        browser_ws_path = port_lines[1] if len(port_lines) > 1 else ""
        page_ws_url = ""
        while time.monotonic() < startup_deadline and not page_ws_url:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    tabs = json.loads(response.read().decode("utf-8"))
            except Exception:
                tabs = []
            for item in tabs:
                if isinstance(item, dict) and item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                    page_ws_url = str(item["webSocketDebuggerUrl"])
                    break
            if not page_ws_url:
                time.sleep(0.1)
        if not page_ws_url:
            raise BrowserStartupError(f"{target_name} {headless_arg} did not expose a page debugging target.")

        try:
            ws = websocket.create_connection(page_ws_url, timeout=5)
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserStartupError(
                f"{target_name} {headless_arg} could not connect to page CDP websocket: {exc}"
            ) from exc
        try:
            command_id = 0

            def cdp(
                method: str,
                params: Dict[str, Any] | None = None,
                *,
                deadline: float,
            ) -> Dict[str, Any]:
                nonlocal command_id
                command_id += 1
                return _cdp_call(
                    ws,
                    command_id=command_id,
                    method=method,
                    params=params,
                    overall_deadline=deadline,
                    target_name=target_name,
                    headless_arg=headless_arg,
                )

            cdp("Page.enable", deadline=startup_deadline)
            cdp("Runtime.enable", deadline=startup_deadline)
            cdp(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": float(target["device_scale_factor"]),
                    "mobile": True,
                },
                deadline=startup_deadline,
            )
            cdp(
                "Emulation.setUserAgentOverride",
                {"userAgent": target["user_agent"], "platform": target["platform"]},
                deadline=startup_deadline,
            )

            # Browser discovery and CDP initialization can be slow on a busy
            # hosted runner. Navigation gets its own bounded acknowledgement
            # budget; rendering gets a fresh budget only after Chrome accepts
            # the navigation command.
            navigation_deadline = time.monotonic() + DEFAULT_CDP_COMMAND_TIMEOUT_SECONDS
            cdp("Page.navigate", {"url": url}, deadline=navigation_deadline)
            render_deadline = time.monotonic() + max(5, int(render_timeout_seconds))

            expression = f"""
(() => {{
  const required = {json.dumps(list(REQUIRED_TEXT))};
  const html = document.documentElement ? document.documentElement.outerHTML : "";
  const text = document.body ? document.body.innerText : "";
  const haystack = html + "\\n" + text;
  const body = document.body;
  const root = document.documentElement;
  const scrollWidth = Math.max(body ? body.scrollWidth : 0, root ? root.scrollWidth : 0);
  const clientWidth = root ? root.clientWidth : window.innerWidth;
  return {{
    title: document.title,
    url: location.href,
    htmlLength: html.length,
    userAgent: navigator.userAgent,
    width: window.innerWidth,
    height: window.innerHeight,
    devicePixelRatio: window.devicePixelRatio,
    textLength: text.length,
    missing: required.filter((fragment) => !haystack.includes(fragment)),
    horizontalOverflow: scrollWidth > clientWidth + 2,
    scrollWidth,
    clientWidth,
    textSnippet: text.slice(0, 500)
  }};
}})()
"""
            last_value: Dict[str, Any] = {}
            reload_attempts = 0
            next_reload_at = time.monotonic() + 5
            while time.monotonic() < render_deadline:
                response = cdp(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    deadline=render_deadline,
                )
                value = response.get("result", {}).get("result", {}).get("value")
                if isinstance(value, dict):
                    last_value = value
                    if not value.get("missing") and not value.get("horizontalOverflow"):
                        return value
                    if (
                        reload_attempts < 2
                        and time.monotonic() >= next_reload_at
                        and int(value.get("htmlLength") or 0) < 1000
                        and int(value.get("textLength") or 0) == 0
                    ):
                        reload_attempts += 1
                        next_reload_at = time.monotonic() + 10
                        cdp("Page.navigate", {"url": url}, deadline=render_deadline)
                time.sleep(0.25)
            detail = json.dumps(last_value, sort_keys=True) if last_value else "{}"
            raise BrowserRenderTimeoutError(
                f"{target_name} {headless_arg} timed out waiting for the mobile UI to render: {detail}"
            )
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


def browser_mobile_check(
    browser_path: str,
    url: str,
    profile_dir: Path,
    target_name: str,
    target: Dict[str, Any],
    *,
    startup_timeout_seconds: int = DEFAULT_BROWSER_STARTUP_TIMEOUT_SECONDS,
    render_timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    errors = []
    for headless_arg, suffix in (("--headless=new", "-new"), ("--headless", "-legacy")):
        for attempt in range(1, DEFAULT_BROWSER_ATTEMPTS_PER_MODE + 1):
            attempt_suffix = "" if suffix == "-new" and attempt == 1 else f"{suffix}-attempt{attempt}"
            if attempt_suffix:
                attempt_profile_dir = profile_dir.with_name(f"{profile_dir.name}{attempt_suffix}")
            else:
                attempt_profile_dir = profile_dir
            try:
                return _browser_mobile_check_once(
                    browser_path,
                    url,
                    attempt_profile_dir,
                    target_name,
                    target,
                    headless_arg=headless_arg,
                    startup_timeout_seconds=startup_timeout_seconds,
                    render_timeout_seconds=render_timeout_seconds,
                )
            except (BrowserStartupError, BrowserRenderTimeoutError) as exc:
                errors.append(str(exc))
    raise SystemExit("Mobile browser smoke exhausted all attempts: " + " ".join(errors))


def run_smoke(args: argparse.Namespace) -> Dict[str, Any]:
    frontend_dir = args.frontend_dir.resolve()
    if not (frontend_dir / "index.html").exists():
        raise SystemExit(f"React build is missing at {frontend_dir}; run npm run build or python verify.py --frontend-build first.")

    browser_path = find_browser(args.browser_path)
    results: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="marketsentinel-mobile-smoke-", ignore_cleanup_errors=True) as tmpdir:
        temp_root = Path(tmpdir)
        config_path = temp_root / "config.json"
        server: ReactGuiServer | None = None
        thread = None
        try:
            server, thread, base_url = start_server(args.host, args.port, config_path, frontend_dir)
            for target_name in _target_names(args.target):
                target = MOBILE_TARGETS[target_name]
                results[target_name] = {
                    "target": target,
                    "dom_check": browser_mobile_check(
                        browser_path,
                        f"{base_url}/",
                        temp_root / f"{target_name}-profile",
                        target_name,
                        target,
                        startup_timeout_seconds=args.startup_timeout_seconds,
                        render_timeout_seconds=args.render_timeout_seconds,
                    ),
                }
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=5)

    return {"ok": True, "browser": browser_path, "targets": results}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the built React UI with mobile browser emulation.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend-dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--target", default="all", help="Target name, comma-separated targets, or all.")
    parser.add_argument(
        "--startup-timeout-seconds",
        type=int,
        default=DEFAULT_BROWSER_STARTUP_TIMEOUT_SECONDS,
        help="Maximum seconds allowed for each browser process to expose and initialize CDP.",
    )
    parser.add_argument("--render-timeout-seconds", type=int, default=DEFAULT_RENDER_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Print the smoke payload as JSON.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_smoke(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("[ok] Mobile web smoke (" + ", ".join(result["targets"].keys()) + ")")


if __name__ == "__main__":
    main()
