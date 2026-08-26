from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from market_adapters import (
    AdapterRuntime,
    MarketAdapter,
    MarketCapabilities,
    MarketHTTPError,
    MarketMetadata,
    PaperOrderRequest,
    RateLimiter,
)
from market_adapters.errors import MarketConfigurationError
from market_adapters.runtime import DEFAULT_USER_AGENT, load_market_fixture


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload=None,
        text: str = "",
        *,
        raw_body: bytes | None = None,
        headers=None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text
        self.raw_body = raw_body if raw_body is not None else json.dumps(self._payload).encode("utf-8")
        self.headers = dict(headers or {})
        self.closed = False

    def json(self):
        return self._payload

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.raw_body), chunk_size):
            yield self.raw_body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class LiveAdapter(MarketAdapter):
    metadata = MarketMetadata(
        market_id="live_dummy",
        display_name="Live Dummy",
        capabilities=MarketCapabilities(
            live_trading=True,
            paper_trading=True,
            credentials_required=True,
            kyc_required=True,
            region_limited=True,
        ),
    )


class AdapterRuntimeTests(unittest.TestCase):
    def test_http_runtime_adds_headers_timeout_and_params(self) -> None:
        session = FakeSession(FakeResponse(payload={"markets": []}))
        runtime = AdapterRuntime("dummy", {"http_timeout_seconds": 3}, session=session)

        data = runtime.get_json("https://example.test/markets", params={"q": "test"})

        self.assertEqual(data, {"markets": []})
        args, kwargs = session.calls[0]
        self.assertEqual(args, ("GET", "https://example.test/markets"))
        self.assertEqual(kwargs["params"], {"q": "test"})
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertEqual(kwargs["headers"]["User-Agent"], DEFAULT_USER_AGENT)
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    def test_http_runtime_raises_market_http_error_for_bad_status(self) -> None:
        session = FakeSession(FakeResponse(status_code=429, text="rate limited"))
        runtime = AdapterRuntime("dummy", session=session)

        with self.assertRaises(MarketHTTPError) as ctx:
            runtime.get_json("https://example.test/markets")

        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertIn("rate limited", str(ctx.exception))

    def test_http_runtime_bounds_streamed_json_before_decoding(self) -> None:
        body = json.dumps({"events": [{"id": 1}]}).encode("utf-8")
        response = FakeResponse(raw_body=body)
        session = FakeSession(response)
        runtime = AdapterRuntime("dummy", session=session)

        data = runtime.get_json(
            "https://example.test/events", max_response_bytes=len(body)
        )

        self.assertEqual(data, {"events": [{"id": 1}]})
        self.assertTrue(session.calls[0][1]["stream"])
        self.assertTrue(response.closed)

        oversized = FakeResponse(raw_body=body)
        with self.assertRaisesRegex(MarketHTTPError, "byte cap"):
            AdapterRuntime("dummy", session=FakeSession(oversized)).get_json(
                "https://example.test/events", max_response_bytes=len(body) - 1
            )
        self.assertTrue(oversized.closed)

        declared = FakeResponse(raw_body=body, headers={"Content-Length": str(len(body))})
        with self.assertRaisesRegex(MarketHTTPError, "byte cap"):
            AdapterRuntime("dummy", session=FakeSession(declared)).get_json(
                "https://example.test/events", max_response_bytes=len(body) - 1
            )
        self.assertTrue(declared.closed)

        parser_failures = (
            (
                b'{"n":' + (b"1" * 5000) + b"}",
                ValueError("integer string conversion limit exceeded"),
            ),
            ((b"[" * 2000) + b"0" + (b"]" * 2000), RecursionError("too deeply nested")),
        )
        for malformed_body, parser_error in parser_failures:
            with self.subTest(parser_error=type(parser_error).__name__):
                malformed = FakeResponse(raw_body=malformed_body)
                with patch(
                    "market_adapters.runtime.json.loads", side_effect=parser_error
                ), self.assertRaisesRegex(MarketHTTPError, "valid JSON"):
                    AdapterRuntime("dummy", session=FakeSession(malformed)).get_json(
                        "https://example.test/events",
                        max_response_bytes=len(malformed_body),
                    )
                self.assertTrue(malformed.closed)

    def test_rate_limiter_uses_configured_delay_without_real_sleep(self) -> None:
        clock_values = [0.0, 0.25, 0.25]
        sleeps = []
        limiter = RateLimiter(
            1.0,
            clock=lambda: clock_values.pop(0),
            sleeper=lambda seconds: sleeps.append(seconds),
        )

        first_delay = limiter.wait()
        second_delay = limiter.wait()

        self.assertEqual(first_delay, 0.0)
        self.assertEqual(second_delay, 0.75)
        self.assertEqual(sleeps, [0.75])

    def test_runtime_resolves_credentials_from_config_without_logging_secret(self) -> None:
        runtime = AdapterRuntime("dummy", {"api_key": "secret-value"})

        credential = runtime.resolve_credential("api_key", ("DUMMY_API_KEY",), required=True)

        self.assertIsNotNone(credential)
        self.assertEqual(credential.value, "secret-value")
        self.assertEqual(credential.source, "config:api_key")
        self.assertEqual(credential.redacted, "***")
        self.assertNotIn("secret-value", str(runtime.describe()))

    def test_runtime_resolves_credentials_from_environment(self) -> None:
        runtime = AdapterRuntime("dummy")

        with patch.dict(os.environ, {"DUMMY_API_KEY": "from-env"}):
            credential = runtime.resolve_credential("api_key", ("DUMMY_API_KEY",), required=True)

        self.assertIsNotNone(credential)
        self.assertEqual(credential.value, "from-env")
        self.assertEqual(credential.source, "env:DUMMY_API_KEY")

    def test_runtime_missing_required_credential_is_clear(self) -> None:
        runtime = AdapterRuntime("dummy")

        with self.assertRaises(MarketConfigurationError) as ctx:
            runtime.resolve_credential("api_key", ("DUMMY_API_KEY",), required=True)

        self.assertIn("Missing required credential", str(ctx.exception))
        self.assertIn("DUMMY_API_KEY", str(ctx.exception))

    def test_market_fixture_loader_reads_offline_json(self) -> None:
        fixture = load_market_fixture("polymarket", "market")

        self.assertEqual(fixture["id"], "market-1")
        self.assertIn("clobTokenIds", fixture)

    def test_base_adapter_health_includes_runtime_metadata(self) -> None:
        adapter = MarketAdapter({"http_timeout_seconds": 2, "min_request_interval_seconds": 0.5})
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(health["adapter"], "MarketAdapter")
        self.assertEqual(health["runtime"]["timeout_seconds"], 2.0)
        self.assertEqual(health["runtime"]["min_request_interval_seconds"], 0.5)
        self.assertIn("capabilities", health)

    def test_base_adapter_live_gate_is_disabled_by_default(self) -> None:
        adapter = MarketAdapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.ensure_live_trading_enabled()

        enabled_adapter = MarketAdapter({"live_trading_enabled": "true", "live_trading_confirmed": "true"})
        enabled_adapter.ensure_live_trading_enabled()

    def test_live_preflight_requires_acknowledgement_and_honors_kill_switch(self) -> None:
        order = PaperOrderRequest("live_dummy", "contract-1", "BUY", 2.0, 0.4)

        with self.assertRaises(MarketConfigurationError) as ack_ctx:
            LiveAdapter({"live_trading_enabled": True}).preflight_live_order(order)
        self.assertIn("acknowledgement", str(ack_ctx.exception))

        with self.assertRaises(MarketConfigurationError) as kill_ctx:
            LiveAdapter(
                {
                    "live_trading_enabled": True,
                    "live_trading_confirmed": True,
                    "live_trading_kill_switch": True,
                }
            ).preflight_live_order(order)
        self.assertIn("kill switch", str(kill_ctx.exception))

    def test_live_preflight_applies_size_notional_caps_and_returns_redacted_audit(self) -> None:
        adapter = LiveAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": 10,
                "live_trading_max_notional": 5,
            }
        )

        preflight = adapter.preflight_live_order(
            PaperOrderRequest(
                "live_dummy",
                "contract-1",
                "BUY",
                4.0,
                0.5,
                {"private_key": "secret", "client_order_id": "client-1"},
            )
        )

        self.assertEqual(preflight["market_id"], "live_dummy")
        self.assertEqual(preflight["approx_notional"], 4.0)
        self.assertEqual(preflight["exposure_model"], "full_size_upper_bound")
        self.assertEqual(preflight["metadata_keys"], ["client_order_id", "private_key"])
        self.assertIn("credentials_required", preflight["warnings"])
        self.assertIn("kyc_required", preflight["warnings"])
        self.assertIn("region_limited", preflight["warnings"])
        self.assertNotIn("secret", str(preflight))

        with self.assertRaises(MarketConfigurationError) as size_ctx:
            adapter.preflight_live_order(PaperOrderRequest("live_dummy", "contract-1", "BUY", 11.0, 0.4))
        self.assertIn("size", str(size_ctx.exception))

        with self.assertRaises(MarketConfigurationError) as notional_ctx:
            adapter.preflight_live_order(PaperOrderRequest("live_dummy", "contract-1", "BUY", 6.0, 0.5))
        self.assertIn("notional", str(notional_ctx.exception))

        with self.assertRaises(MarketConfigurationError) as high_price_ctx:
            adapter.preflight_live_order(PaperOrderRequest("live_dummy", "contract-1", "BUY", 3.0, 2.0))
        self.assertIn("notional 6", str(high_price_ctx.exception))

    def test_live_preflight_rejects_noncanonical_contracts_and_order_sides(self) -> None:
        adapter = LiveAdapter({"live_trading_enabled": True, "live_trading_confirmed": True})

        for contract_id in ("", " ", " contract-1", "contract-1 "):
            with self.subTest(contract_id=repr(contract_id)):
                with self.assertRaisesRegex(MarketConfigurationError, "canonical contract id"):
                    adapter.preflight_live_order(PaperOrderRequest("live_dummy", contract_id, "BUY", 1.0, 0.4))

        for side in ("", "buy", "BUY ", "HOLD"):
            with self.subTest(side=repr(side)):
                with self.assertRaisesRegex(MarketConfigurationError, "side must be one of: BUY, SELL"):
                    adapter.preflight_live_order(PaperOrderRequest("live_dummy", "contract-1", side, 1.0, 0.4))

    def test_base_adapter_order_market_gate(self) -> None:
        adapter = MarketAdapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.ensure_order_market(
                PaperOrderRequest(
                    market_id="other",
                    contract_id="contract-1",
                    side="BUY",
                    size=1.0,
                )
            )


if __name__ == "__main__":
    unittest.main()
