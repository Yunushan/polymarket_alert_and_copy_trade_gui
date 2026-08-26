from __future__ import annotations

import json
import socket
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from market_adapters.azuro import AzuroAdapter
from market_adapters.errors import MarketConfigurationError, MarketHTTPError
from market_adapters.limitless import LimitlessAdapter
from market_adapters.opinion import OpinionAdapter
from market_adapters.outbound import (
    MAX_OUTBOUND_URL_LENGTH,
    OUTBOUND_ENDPOINT_SETTING_KEYS,
    OUTBOUND_POLICY_SETTING_KEYS,
    OUTBOUND_PRIVATE_ORIGINS_ENV,
    OutboundEndpointPolicy,
    is_outbound_endpoint_setting,
    validate_outbound_url,
)
from market_adapters.runtime import AdapterRuntime
from market_adapters.sx_bet import SxBetAdapter


ROOT = Path(__file__).resolve().parents[1]


def resolver_for(*addresses: str):
    def resolve(host: str, port: int, *, type: int):
        records = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            records.append((family, type, socket.IPPROTO_TCP, "", sockaddr))
        return records

    return resolve


class FakeOpinionRESTResponse:
    def __init__(self, response):
        self.response = response
        self.status = response.status
        self.reason = response.reason
        self.data = None

    def read(self):
        self.data = self.response.data
        return self.data


def fake_opinion_modules(client_type, *, sdk_version="0.7.0", api_version="0.4.0"):
    sdk_module = types.ModuleType("opinion_clob_sdk")
    sdk_module.__version__ = sdk_version
    sdk_module.Client = client_type
    api_module = types.ModuleType("opinion_api")
    api_module.__path__ = []
    api_module.__version__ = api_version
    rest_module = types.ModuleType("opinion_api.rest")
    rest_module.RESTResponse = FakeOpinionRESTResponse
    api_module.rest = rest_module
    return {
        "opinion_clob_sdk": sdk_module,
        "opinion_api": api_module,
        "opinion_api.rest": rest_module,
    }


class OutboundEndpointTests(unittest.TestCase):
    def test_public_https_and_wss_urls_are_canonicalized(self) -> None:
        policy = OutboundEndpointPolicy(resolver=resolver_for("93.184.216.34"))

        self.assertEqual(
            validate_outbound_url(
                "HTTPS://Example.COM:443/api/events?q=open",
                setting_key="api_base_url",
                policy=policy,
            ),
            "https://example.com/api/events?q=open",
        )
        self.assertEqual(
            validate_outbound_url(
                "WSS://Streams.Example.COM:443/feed",
                setting_key="websocket_url",
                kind="websocket",
                policy=policy,
            ),
            "wss://streams.example.com/feed",
        )

    def test_plaintext_and_unsafe_url_syntax_are_rejected(self) -> None:
        invalid_urls = (
            "http://example.com/api",
            "ftp://example.com/api",
            "https://user:secret@example.com/api",
            "https://example.com/api#fragment",
            " https://example.com/api",
            "https://example.com\\@127.0.0.1/api",
            "https://example.com:0/api",
            "https://bad_host.example/api",
            "https://example.com/" + ("x" * MAX_OUTBOUND_URL_LENGTH),
        )
        for url in invalid_urls:
            with self.subTest(url=url[:80]):
                with self.assertRaises(MarketConfigurationError):
                    validate_outbound_url(url, resolve_addresses=False)

        with self.assertRaisesRegex(MarketConfigurationError, "base URL"):
            validate_outbound_url(
                "https://example.com/api?token=secret",
                base_url=True,
                resolve_addresses=False,
            )

    def test_private_literal_and_resolved_addresses_are_rejected(self) -> None:
        private_urls = (
            "https://127.0.0.1/api",
            "https://[::1]/api",
            "https://10.0.0.10/api",
            "https://169.254.169.254/latest/meta-data",
            "https://100.64.0.1/api",
        )
        for url in private_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(MarketConfigurationError, "non-public"):
                    validate_outbound_url(url, resolve_addresses=False)

        private_dns = OutboundEndpointPolicy(resolver=resolver_for("192.168.1.10"))
        with self.assertRaisesRegex(MarketConfigurationError, "non-public"):
            validate_outbound_url("https://api.example.com", policy=private_dns)

        mixed_dns = OutboundEndpointPolicy(
            resolver=resolver_for("93.184.216.34", "127.0.0.1")
        )
        with self.assertRaisesRegex(MarketConfigurationError, "non-public"):
            validate_outbound_url("https://api.example.com", policy=mixed_dns)

    def test_hostname_resolution_fails_closed(self) -> None:
        def failing_resolver(host: str, port: int, *, type: int):
            raise socket.gaierror("not found")

        policy = OutboundEndpointPolicy(resolver=failing_resolver)
        with self.assertRaisesRegex(MarketConfigurationError, "could not be resolved"):
            validate_outbound_url("https://missing.example", policy=policy)

    def test_exact_private_origin_allowlist_supports_local_gateways(self) -> None:
        policy = OutboundEndpointPolicy.from_environment(
            {
                OUTBOUND_PRIVATE_ORIGINS_ENV: (
                    "http://127.0.0.1:8545, https://localhost:5000, "
                    "https://10.0.0.5:443"
                )
            },
            resolver=resolver_for("127.0.0.1"),
        )

        self.assertEqual(
            validate_outbound_url(
                "http://127.0.0.1:8545",
                setting_key="evm_rpc_url",
                policy=policy,
            ),
            "http://127.0.0.1:8545",
        )
        self.assertEqual(
            validate_outbound_url(
                "https://localhost:5000/v1/api",
                setting_key="ibkr_api_base_url",
                base_url=True,
                policy=policy,
            ),
            "https://localhost:5000/v1/api",
        )
        self.assertEqual(
            validate_outbound_url(
                "https://10.0.0.5/api",
                setting_key="rpc_url",
                policy=policy,
                resolve_addresses=False,
            ),
            "https://10.0.0.5/api",
        )

        for url in (
            "http://127.0.0.1:8546",
            "http://localhost:8545",
            "https://127.0.0.1:5000/v1/api",
        ):
            with self.subTest(url=url):
                with self.assertRaises(MarketConfigurationError):
                    validate_outbound_url(url, policy=policy, resolve_addresses=False)

    def test_plaintext_private_network_origin_cannot_be_allowlisted(self) -> None:
        with self.assertRaisesRegex(MarketConfigurationError, "plaintext only for loopback"):
            OutboundEndpointPolicy.from_environment(
                {OUTBOUND_PRIVATE_ORIGINS_ENV: "http://10.0.0.5:8545"}
            )

    def test_policy_is_loaded_as_an_immutable_snapshot(self) -> None:
        environ = {OUTBOUND_PRIVATE_ORIGINS_ENV: "https://localhost:5000"}
        policy = OutboundEndpointPolicy.from_environment(environ)
        environ[OUTBOUND_PRIVATE_ORIGINS_ENV] = "https://127.0.0.1:5000"

        self.assertEqual(policy.private_origins, frozenset({"https://localhost:5000"}))
        with self.assertRaises(AttributeError):
            policy.private_origins = frozenset()  # type: ignore[misc]

    def test_runtime_loads_private_origin_policy_after_environment_initialization(self) -> None:
        with patch.dict(
            "market_adapters.outbound.os.environ",
            {OUTBOUND_PRIVATE_ORIGINS_ENV: "http://127.0.0.1:8545"},
            clear=False,
        ):
            runtime = AdapterRuntime("late-policy")
            validated = runtime.validate_endpoint(
                "http://127.0.0.1:8545",
                setting_key="evm_rpc_url",
                base_url=True,
            )

        self.assertEqual(validated, "http://127.0.0.1:8545")

    def test_endpoint_inventory_covers_example_configuration(self) -> None:
        payload = json.loads((ROOT / "data" / "config.example.json").read_text(encoding="utf-8"))
        discovered = set()
        for market in payload.get("markets", {}).values():
            settings = market.get("settings", {}) if isinstance(market, dict) else {}
            for key in settings:
                if key.endswith(("_url", "_host", "_base_url")) or "allow_custom" in key:
                    discovered.add(key)

        inventory = OUTBOUND_ENDPOINT_SETTING_KEYS | OUTBOUND_POLICY_SETTING_KEYS
        self.assertEqual(discovered - inventory, set())
        self.assertTrue(is_outbound_endpoint_setting("kalshi_api_base_url"))
        self.assertTrue(is_outbound_endpoint_setting("KALSHI-API-BASE-URL"))
        self.assertTrue(is_outbound_endpoint_setting("Kalshi.API Base URL"))
        self.assertTrue(is_outbound_endpoint_setting("IEM-Historical Markets"))
        self.assertTrue(is_outbound_endpoint_setting("scicast_allow_custom_base_url"))
        self.assertTrue(is_outbound_endpoint_setting("SCICAST-ALLOW-CUSTOM-BASE-URL"))
        self.assertFalse(is_outbound_endpoint_setting("live_trading_enabled"))

    def test_adapter_sources_do_not_bypass_bounded_runtime_requests(self) -> None:
        offenders = []
        for path in (ROOT / "market_adapters").glob("*.py"):
            if path.name == "runtime.py":
                continue
            if "runtime.session.request" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_configurable_websocket_connection_urls_reject_private_targets(self) -> None:
        cases = (
            (
                AzuroAdapter({"azuro_ws_url": "wss://127.0.0.1/feed"}),
                {"game_ids": ["game-1"]},
            ),
            (
                LimitlessAdapter({"limitless_ws_url": "wss://127.0.0.1/feed"}),
                {"market_slugs": ["market-1"]},
            ),
            (
                SxBetAdapter({"sx_bet_ws_url": "wss://127.0.0.1/feed"}),
                {"event_ids": ["event-1"]},
            ),
            (
                SxBetAdapter({"sx_bet_api_base_url": "https://127.0.0.1"}),
                {"event_ids": ["event-1"]},
            ),
        )
        for adapter, kwargs in cases:
            with self.subTest(adapter=adapter.market_id, config=adapter.config):
                with self.assertRaisesRegex(MarketConfigurationError, "non-public"):
                    adapter.websocket_connection_info(**kwargs)

    def test_opinion_sdk_validates_configurable_host_and_rpc_before_construction(self) -> None:
        class NoNetworkSession:
            def request(self, *args, **kwargs):
                raise AssertionError("Opinion client validation must not issue HTTP requests")

        created = []
        clients = []

        class FakeClient:
            def __init__(self, **kwargs):
                created.append(kwargs)
                clients.append(self)
                self.api_client = SimpleNamespace(rest_client=object())
                self.market_api = SimpleNamespace(api_client=self.api_client)
                self.user_api = SimpleNamespace(api_client=self.api_client)

        credentials = {
            "opinion_api_key": "api-key",
            "opinion_private_key": "private-key",
            "opinion_multi_sig_address": "0x1111111111111111111111111111111111111111",
            "opinion_rpc_url": "https://rpc.example.test",
        }
        runtime = AdapterRuntime(
            "opinion_labs",
            credentials,
            session=NoNetworkSession(),
        )

        with patch.dict(sys.modules, fake_opinion_modules(FakeClient)):
            adapter = OpinionAdapter(
                {
                    **credentials,
                    "opinion_clob_host": "https://clob.example.test:8443",
                },
                runtime=runtime,
            )
            client = adapter._create_clob_client()

            self.assertEqual(created[0]["host"], "https://clob.example.test:8443")
            self.assertEqual(created[0]["rpc_url"], "https://rpc.example.test")
            self.assertIs(client, clients[0])
            self.assertEqual(type(client.api_client.rest_client).__name__, "_ManagedOpinionSDKRestClient")

            private_host = OpinionAdapter(
                {**credentials, "opinion_clob_host": "https://127.0.0.1:8443"},
                runtime=runtime,
            )
            with self.assertRaisesRegex(MarketConfigurationError, "non-public"):
                private_host._create_clob_client()

        self.assertEqual(len(created), 1)

    def test_opinion_sdk_managed_transport_enforces_origin_redirect_timeout_and_body_cap(self) -> None:
        class Response:
            def __init__(self, body=b'{"ok":true}', *, status=200, headers=None):
                self.body = body
                self.status_code = status
                self.headers = dict(headers or {})
                self.text = body.decode("utf-8", errors="replace")
                self.closed = False

            def iter_content(self, *, chunk_size):
                for index in range(0, len(self.body), max(1, chunk_size)):
                    yield self.body[index : index + chunk_size]

            def close(self):
                self.closed = True

        class RecordingSession:
            def __init__(self):
                self.calls = []
                self.response = Response()

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return self.response

        clients = []

        class FakeClient:
            def __init__(self, **kwargs):
                del kwargs
                clients.append(self)
                self.api_client = SimpleNamespace(rest_client=object())
                self.market_api = SimpleNamespace(api_client=self.api_client)
                self.user_api = SimpleNamespace(api_client=self.api_client)

        credentials = {
            "opinion_api_key": "api-key",
            "opinion_private_key": "private-key",
            "opinion_multi_sig_address": "0x1111111111111111111111111111111111111111",
            "opinion_rpc_url": "https://rpc.example.test",
            "opinion_clob_host": "https://clob.example.test:8443",
            "http_timeout_seconds": 7,
            "http_max_response_bytes": 32,
        }
        session = RecordingSession()
        runtime = AdapterRuntime("opinion_labs", credentials, session=session)

        with patch.dict(sys.modules, fake_opinion_modules(FakeClient)):
            transport = OpinionAdapter(credentials, runtime=runtime)._create_clob_client().api_client.rest_client
            response = transport.request(
                "POST",
                "https://clob.example.test:8443/openapi/order",
                headers={"apikey": "api-key", "Content-Type": "application/json"},
                body={"order": "signed"},
                post_params={},
            )
            self.assertEqual(response.read(), b'{"ok":true}')
            self.assertEqual(len(session.calls), 1)
            method, url, kwargs = session.calls[0]
            self.assertEqual((method, url), ("POST", "https://clob.example.test:8443/openapi/order"))
            self.assertEqual(kwargs["timeout"], 7.0)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["json"], {"order": "signed"})
            self.assertEqual(kwargs["headers"]["apikey"], "api-key")

            with self.assertRaisesRegex(MarketConfigurationError, "origin"):
                transport.request(
                    "GET",
                    "https://redirect.example.test/openapi/order",
                    headers={"apikey": "api-key"},
                )
            self.assertEqual(len(session.calls), 1)

            session.response = Response(status=302, headers={"Location": "https://redirect.example.test"})
            with self.assertRaisesRegex(MarketHTTPError, "redirects are disabled"):
                transport.request(
                    "GET",
                    "https://clob.example.test:8443/openapi/order",
                    headers={"apikey": "api-key"},
                )
            self.assertTrue(session.response.closed)
            self.assertEqual(len(session.calls), 2)

            session.response = Response(b"x" * 33, headers={"Content-Length": "33"})
            with self.assertRaisesRegex(MarketHTTPError, "byte cap"):
                transport.request(
                    "GET",
                    "https://clob.example.test:8443/openapi/order",
                    headers={"apikey": "api-key"},
                )
            self.assertTrue(session.response.closed)

            session.response = Response(b"x" * 33)
            with self.assertRaisesRegex(MarketHTTPError, "byte cap"):
                transport.request(
                    "GET",
                    "https://clob.example.test:8443/openapi/order",
                    headers={"apikey": "api-key"},
                )
            self.assertTrue(session.response.closed)

    def test_opinion_sdk_fails_closed_on_version_layout_timeout_and_header_mismatch(self) -> None:
        credentials = {
            "opinion_api_key": "api-key",
            "opinion_private_key": "private-key",
            "opinion_multi_sig_address": "0x1111111111111111111111111111111111111111",
            "opinion_rpc_url": "https://rpc.example.test",
        }

        class WrongLayoutClient:
            def __init__(self, **kwargs):
                del kwargs

        runtime = AdapterRuntime("opinion_labs", credentials, session=object())
        with patch.dict(sys.modules, fake_opinion_modules(WrongLayoutClient)):
            with self.assertRaisesRegex(MarketConfigurationError, "transport layout"):
                OpinionAdapter(credentials, runtime=runtime)._create_clob_client()

        with patch.dict(
            sys.modules,
            fake_opinion_modules(WrongLayoutClient, sdk_version="0.8.0"),
        ):
            with self.assertRaisesRegex(MarketConfigurationError, "reviewed opinion-clob-sdk"):
                OpinionAdapter(credentials, runtime=runtime)._create_clob_client()

        class FakeClient:
            def __init__(self, **kwargs):
                del kwargs
                self.api_client = SimpleNamespace(rest_client=object())
                self.market_api = SimpleNamespace(api_client=self.api_client)
                self.user_api = SimpleNamespace(api_client=self.api_client)

        invalid_timeout_runtime = AdapterRuntime(
            "opinion_labs",
            {**credentials, "http_timeout_seconds": 31},
            session=object(),
        )
        with patch.dict(sys.modules, fake_opinion_modules(FakeClient)):
            with self.assertRaisesRegex(MarketConfigurationError, "between 0 and 30"):
                OpinionAdapter(
                    {**credentials, "http_timeout_seconds": 31},
                    runtime=invalid_timeout_runtime,
                )._create_clob_client()

            valid_runtime = AdapterRuntime("opinion_labs", credentials, session=object())
            transport = OpinionAdapter(credentials, runtime=valid_runtime)._create_clob_client().api_client.rest_client
            with self.assertRaisesRegex(MarketConfigurationError, "expected API key"):
                transport.request(
                    "GET",
                    "https://proxy.opinion.trade:8443/openapi/order",
                    headers={"apikey": "different-key"},
                )


if __name__ == "__main__":
    unittest.main()
