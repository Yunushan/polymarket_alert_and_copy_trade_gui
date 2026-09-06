from __future__ import annotations

import argparse
import json
import math
import os
import time
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_PROBE_RESPONSE_BYTES = 1024 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    # Python 3.10 lacks the stdlib 308 dispatch alias.
    http_error_308 = HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise RuntimeError("health and deployment probe redirects are forbidden")


def open_probe(request: Request, timeout: float):
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("probe socket timeout must be finite and greater than zero")
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def read_probe_body(response) -> bytes:
    headers = getattr(response, "headers", {})
    length = headers.get("Content-Length")
    if length is not None:
        try:
            declared_bytes = int(length)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("probe response has an invalid Content-Length") from exc
        if declared_bytes < 0 or declared_bytes > MAX_PROBE_RESPONSE_BYTES:
            raise RuntimeError("probe response exceeds the byte limit")
    body = response.read(MAX_PROBE_RESPONSE_BYTES + 1)
    if len(body) > MAX_PROBE_RESPONSE_BYTES:
        raise RuntimeError("probe response exceeds the byte limit")
    return body


def read_health_payload(response) -> dict:
    if response.status != 200:
        raise RuntimeError(f"health endpoint returned HTTP {response.status}")
    try:
        payload = json.loads(read_probe_body(response).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RuntimeError("health endpoint returned invalid JSON") from exc
    return validate_health_payload(payload)


def check_health(url: str, token: str, timeout: float) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    with open_probe(request, timeout=timeout) as response:
        return read_health_payload(response)


def validate_health_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError("health endpoint did not report status=ok")
    version = payload.get("api_version")
    if not isinstance(version, str) or not version.strip() or version == "unknown":
        raise RuntimeError("health endpoint did not report a usable api_version")
    readiness = payload.get("readiness")
    if "readiness" in payload:
        if not isinstance(readiness, dict) or readiness.get("ready") is not True:
            status = readiness.get("status") if isinstance(readiness, dict) else "invalid"
            raise RuntimeError(f"health endpoint reported service readiness={status or 'degraded'}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the local MarketSentinel web API health endpoint.")
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/health")
    parser.add_argument("--token", default=os.environ.get("MARKET_SENTINEL_API_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=12)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    args = parser.parse_args()

    last_error: Exception | None = None
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            payload = check_health(args.url, args.token, args.timeout)
            print(
                f"[ok] service health on attempt {attempt}: "
                f"version={payload['api_version']}; {payload.get('message', 'ok')}"
            )
            return 0
        except (OSError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < max(1, args.retries):
                time.sleep(max(0.0, args.retry_delay))
    raise SystemExit(f"Service health check failed after {args.retries} attempts: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
