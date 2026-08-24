from __future__ import annotations

import importlib.metadata as importlib_metadata
import json
import io
import os
import threading
import tempfile
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core.models import AppConfig, CopyTradeSettings, PaperTradeRecord, PriceAlert, WalletWatch
from core.storage import ConfigLoadError, load_config, save_config
from market_adapters.base import MarketAdapter
from market_adapters.types import (
    MarketCapabilities,
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
    MarketTrade,
)
from market_adapters.errors import UnsupportedFeatureError
from polymarket.analytics_cache import POLYMARKET_MDD_AUDIT_KIND, store_analytics_artifact
from polymarket.gamma import ProfileResult
from polymarket.http_client import PolymarketRateLimitError
from polymarket.mdd import MDD_METHOD_MARK_REPLAY, MDD_METHOD_V2
from web_api import (
    activity_key,
    _fetch_polymarket_leaderboard_scan_rows,
    _read_json_body,
    add_wallet_watch,
    alert_from_payload,
    alerts_payload,
    api_error_payload,
    app_state_payload,
    apply_copy_settings_patch,
    apply_config_patch,
    apply_market_patch,
    copy_payload,
    copy_preview_payload,
    copy_trade_preview_from_activity,
    configured_allowed_origins,
    delete_alert,
    delete_wallet_watch,
    health_payload,
    history_refill_payload,
    is_loopback_host,
    live_preflight_payload,
    live_safety_payload,
    markets_payload,
    market_candles_payload,
    market_account_payload,
    market_position_intent_payload,
    market_order_management_payload,
    market_contracts_payload,
    market_events_payload,
    market_orderbook_payload,
    market_price_payload,
    market_trades_payload,
    market_support_payload,
    paper_payload,
    paper_order_impact,
    paper_order_from_payload,
    paper_quote_limit_payload,
    paper_quote_payload,
    paper_position_rows,
    polymarket_clob_readiness_payload,
    polymarket_live_validation_decision_store_payload,
    polymarket_live_validation_decisions_payload,
    polymarket_live_validation_promotion_proposal_payload,
    polymarket_live_validation_promotion_proposal_snapshot_payload,
    polymarket_live_validation_promotion_proposal_snapshot_diff_payload,
    polymarket_live_validation_promotion_proposal_snapshot_store_payload,
    polymarket_live_validation_promotion_proposal_snapshots_payload,
    polymarket_live_validation_report_payload,
    polymarket_live_validation_report_purge_payload,
    polymarket_live_validation_report_review_payload,
    polymarket_live_validation_report_store_payload,
    polymarket_live_validation_reports_payload,
    polymarket_live_validation_payload,
    polymarket_leaderboard_payload,
    polymarket_mdd_export_payload,
    polymarket_user_mdd_payload,
    polymarket_user_search_payload,
    project_version,
    position_refill_payload,
    poll_wallet_activity,
    refresh_selected_paper_mark,
    refresh_alert_price,
    ReactGuiHandler,
    ReactGuiServer,
    _normalize_allowed_origin,
    _safe_attachment_filename,
    _safe_http_header_value,
    static_cache_control,
    run_server,
    submit_paper_order,
    update_wallet_watch,
    wallets_payload,
)


WALLET = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WALLET_2 = "0xcccccccccccccccccccccccccccccccccccccccc"
ROOT = Path(__file__).resolve().parent.parent
LIVE_REPORT_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "polymarket" / "live_reports"


class FakePaperAdapter(MarketAdapter):
    metadata = MarketMetadata(
        market_id="kalshi",
        display_name="Kalshi",
        capabilities=MarketCapabilities(
            price_reading=True,
            orderbook_reading=True,
            alerts=True,
            paper_trading=True,
            live_trading=True,
        ),
    )

    def __init__(self) -> None:
        super().__init__({})
        self.prices: list[str] = []
        self.orderbooks: list[str] = []
        self.orders: list[PaperOrderRequest] = []

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.prices.append(contract_id)
        return PriceSnapshot(
            market_id="kalshi",
            contract_id=contract_id,
            last=0.62,
            bid=0.60,
            ask=0.64,
            source="test",
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.orderbooks.append(contract_id)
        return OrderBookSnapshot(
            market_id="kalshi",
            contract_id=contract_id,
            bids=[OrderBookLevel(price=0.58, size=12)],
            asks=[OrderBookLevel(price=0.66, size=15)],
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.orders.append(order)
        return PaperOrderResult(
            market_id=order.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message="accepted",
            filled_size=order.size,
            average_price=order.limit_price,
            raw={"dry_run": True},
        )


class FakePolymarketAdapter(MarketAdapter):
    metadata = MarketMetadata(
        market_id="polymarket",
        display_name="Polymarket",
        capabilities=MarketCapabilities(
            price_reading=True,
            alerts=True,
            orderbook_reading=True,
            live_trading=True,
            copy_trading=True,
        ),
    )

    def __init__(self) -> None:
        super().__init__({})
        self.prices: list[str] = []
        self.orderbooks: list[str] = []

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.prices.append(contract_id)
        return PriceSnapshot(
            market_id="polymarket",
            contract_id=contract_id,
            last=0.61,
            bid=0.60,
            ask=0.64,
            midpoint=0.62,
            source="test-polymarket",
        )

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.orderbooks.append(contract_id)
        return OrderBookSnapshot(
            market_id="polymarket",
            contract_id=contract_id,
            bids=[OrderBookLevel(price=0.40, size=20)],
            asks=[OrderBookLevel(price=0.45, size=25)],
        )


class FakeOpinionCopyAdapter(FakePolymarketAdapter):
    metadata = MarketMetadata(
        market_id="opinion_labs",
        display_name="Opinion Labs",
        capabilities=MarketCapabilities(
            price_reading=True,
            alerts=True,
            orderbook_reading=True,
            paper_trading=True,
            copy_trading=True,
        ),
    )

    def list_activity(self, wallet: str, *, limit: int = 25) -> list[dict]:
        return [
            {
                "transactionHash": "opinion-tx-1",
                "timestamp": 101,
                "proxyWallet": wallet,
                "asset": "77:YES:0xyes",
                "side": "BUY",
                "price": "0.44",
                "size": "10",
                "slug": "77",
                "outcome": "Yes",
            }
        ][:limit]


class FakeMyriadCopyAdapter(FakePolymarketAdapter):
    metadata = MarketMetadata(
        market_id="myriad_markets",
        display_name="Myriad Markets",
        capabilities=MarketCapabilities(
            price_reading=True,
            alerts=True,
            orderbook_reading=True,
            paper_trading=True,
            copy_trading=True,
        ),
    )

    def list_activity(self, wallet: str, *, limit: int = 25) -> list[dict]:
        return [
            {
                "transactionHash": "myriad-tx-1",
                "timestamp": 201,
                "proxyWallet": wallet,
                "asset": "501:1",
                "side": "BUY",
                "price": 0.61,
                "size": 12.2,
                "value": 12.2,
                "shares": 20.0,
                "slug": "btc-above-100k-2026",
                "outcome": "Yes",
            }
        ][:limit]


class FakeAzuroCopyAdapter(FakePolymarketAdapter):
    metadata = MarketMetadata(
        market_id="azuro",
        display_name="Azuro",
        capabilities=MarketCapabilities(
            price_reading=True,
            alerts=True,
            paper_trading=True,
            live_trading=True,
            copy_trading=True,
        ),
    )


class FakeRegistry:
    def __init__(self, adapter: MarketAdapter) -> None:
        self.adapter = adapter

    def create(self, _market_id: str, _settings=None) -> MarketAdapter:
        self.adapter.config = dict(_settings or {})
        self.adapter.runtime = self.adapter._create_runtime()
        return self.adapter


class SecretFailRegistry:
    def create(self, _market_id: str, _settings=None) -> MarketAdapter:
        raise RuntimeError("adapter failed with super-secret-token")


class FakeBodyHandler:
    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self.headers = {"Content-Length": content_length if content_length is not None else str(len(body))}
        self.rfile = io.BytesIO(body)


class WebApiTests(unittest.TestCase):
    @staticmethod
    def _accounting_zip(equity_csv: str, positions_csv: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("equity.csv", equity_csv)
            archive.writestr("positions.csv", positions_csv)
        return buffer.getvalue()

    def _serve_api(self, config_path: Path, frontend_dir: Path, *, api_token: str = ""):
        with patch("web_api.DEFAULT_FRONTEND_DIR", frontend_dir):
            server = ReactGuiServer(
                ("127.0.0.1", 0),
                ReactGuiHandler,
                config_path=config_path,
                frontend_dir=frontend_dir,
                adapter_registry=FakeRegistry(FakePaperAdapter()),
                api_token=api_token,
            )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def _request_json(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        payload: object | None = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        data = raw
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(f"{base_url}{path}", data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def _request_raw(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request = Request(f"{base_url}{path}", headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()

    def test_activity_key_prefers_transaction_and_activity_ids(self) -> None:
        self.assertEqual(activity_key({"transactionHash": "0xABC"}), "tx:0xabc")
        self.assertEqual(
            activity_key({"activity_id": "Context:0xabc:0x1"}),
            "activity-id:context:0xabc:0x1",
        )

    def test_loopback_detection_and_remote_server_token_gate(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("192.0.2.10"))

        with self.assertRaisesRegex(ValueError, "non-loopback"):
            ReactGuiServer(("0.0.0.0", 0), ReactGuiHandler)

    def test_server_uses_bounded_connection_and_shutdown_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frontend_dir = root / "dist"
            with patch("web_api.DEFAULT_FRONTEND_DIR", frontend_dir):
                server = ReactGuiServer(
                    ("127.0.0.1", 0),
                    ReactGuiHandler,
                    config_path=root / "config.json",
                    frontend_dir=frontend_dir,
                )
            try:
                self.assertTrue(server.allow_reuse_address)
                self.assertTrue(server.daemon_threads)
                self.assertFalse(server.block_on_close)
                self.assertEqual(server.request_queue_size, 32)
            finally:
                server.server_close()

    def test_custom_frontend_directory_is_confined_to_deployment_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            deployment_root = Path(tmpdir)
            frontend_dir = deployment_root / "frontend" / "dist"
            frontend_dir.mkdir(parents=True)
            (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            with patch("web_api._RESOURCE_ROOT", deployment_root):
                server = ReactGuiServer(
                    ("127.0.0.1", 0),
                    ReactGuiHandler,
                    config_path=deployment_root / "config.json",
                    frontend_dir=frontend_dir,
                )
                try:
                    self.assertEqual(server.frontend_dir, frontend_dir.resolve())
                    self.assertEqual(server.static_files["index.html"], (frontend_dir / "index.html").resolve())
                finally:
                    server.server_close()

            outside_dir = Path(tmpdir).parent / f"{Path(tmpdir).name}-outside"
            with self.assertRaisesRegex(ValueError, "deployment resource root"):
                ReactGuiServer(
                    ("127.0.0.1", 0),
                    ReactGuiHandler,
                    config_path=deployment_root / "config.json",
                    frontend_dir=outside_dir,
                )

    def test_configured_allowed_origins_merges_cli_and_environment_values(self) -> None:
        with patch.dict(
            os.environ,
            {"MARKET_SENTINEL_ALLOWED_ORIGINS": "https://analytics.example.com/, invalid, https://ops.example.com/path"},
            clear=False,
        ):
            origins = configured_allowed_origins(["https://console.example", "https://console.example"])

        self.assertEqual(
            origins,
            ["https://console.example", "https://analytics.example.com"],
        )

    def test_server_refuses_to_start_with_a_corrupt_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(ConfigLoadError):
                run_server("127.0.0.1", 0, config_path)

    def test_dynamic_http_header_values_cannot_inject_response_headers(self) -> None:
        injected = 'report.csv\r\nSet-Cookie: compromised=true\n'

        self.assertEqual(_safe_http_header_value(injected), "report.csvSet-Cookie: compromised=true")
        self.assertEqual(_safe_attachment_filename('report".csv\r\nX-Test: injected'), "report.csvX-Test: injected")
        self.assertEqual(_normalize_allowed_origin("https://console.example\r\nX-Test: injected"), "")
        self.assertEqual(_normalize_allowed_origin("https://console.example/"), "https://console.example")

    def test_api_token_and_cors_allowlist_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frontend_dir = root / "frontend"
            frontend_dir.mkdir()
            (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            server, thread, base_url = self._serve_api(root / "config.json", frontend_dir, api_token="test-token")
            try:
                status, payload = self._request_json(base_url, "/api/health")
                self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                self.assertEqual(payload["error"]["code"], "api_token_required")

                status, payload = self._request_json(
                    base_url,
                    "/api/health",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(payload["status"], "ok")

                status, headers, _ = self._request_raw(
                    base_url,
                    "/api/health",
                    headers={"Authorization": "Bearer test-token"},
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertRegex(headers.get("X-Request-ID", ""), r"^[0-9a-f]{24}$")

                status, payload = self._request_json(
                    base_url,
                    "/api/health",
                    headers={"Origin": "https://untrusted.example", "Authorization": "Bearer test-token"},
                )
                self.assertEqual(status, HTTPStatus.FORBIDDEN)
                self.assertEqual(payload["error"]["code"], "cors_origin_forbidden")

                status, headers, _ = self._request_raw(
                    base_url,
                    "/api/health",
                    method="OPTIONS",
                    headers={"Origin": "http://127.0.0.1:5173"},
                )
                self.assertEqual(status, HTTPStatus.NO_CONTENT)
                self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:5173")
                self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
                self.assertEqual(headers.get("Access-Control-Expose-Headers"), "X-Request-ID")

                status, headers, body = self._request_raw(
                    base_url,
                    "/metrics",
                    headers={"Authorization": "Bearer test-token"},
                )
                metrics = body.decode("utf-8")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(headers.get("Content-Type"), "text/plain; version=0.0.4; charset=utf-8")
                self.assertIn("# TYPE market_sentinel_http_requests_total counter", metrics)
                self.assertIn('market_sentinel_http_requests_total{method="GET",status="200"}', metrics)
                self.assertIn("market_sentinel_http_request_duration_seconds_total", metrics)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_api_token_failures_are_rate_limited_and_valid_token_resets_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frontend_dir = root / "frontend"
            frontend_dir.mkdir()
            (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            server, thread, base_url = self._serve_api(root / "config.json", frontend_dir, api_token="test-token")
            try:
                for _ in range(10):
                    status, payload = self._request_json(base_url, "/api/health", headers={"Authorization": "Bearer wrong"})
                    self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                    self.assertEqual(payload["error"]["code"], "api_token_required")

                status, headers, body = self._request_raw(
                    base_url, "/api/health", headers={"Authorization": "Bearer wrong"}
                )
                self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)
                self.assertRegex(headers.get("Retry-After", ""), r"^[1-9][0-9]*$")
                self.assertEqual(json.loads(body.decode("utf-8"))["error"]["code"], "api_token_rate_limited")

                status, _payload = self._request_json(
                    base_url, "/api/health", headers={"Authorization": "Bearer test-token"}
                )
                self.assertEqual(status, HTTPStatus.OK)
                status, payload = self._request_json(base_url, "/api/health", headers={"Authorization": "Bearer wrong"})
                self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
                self.assertEqual(payload["error"]["code"], "api_token_required")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_markets_payload_merges_catalog_with_local_enablement(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        cfg.selected_market_id = "kalshi"

        payload = markets_payload(cfg)

        kalshi = next(market for market in payload["markets"] if market["market_id"] == "kalshi")
        self.assertTrue(kalshi["enabled"])
        self.assertTrue(kalshi["capabilities"]["paper_trading"])
        self.assertEqual(payload["selected_market_id"], "kalshi")
        self.assertGreaterEqual(payload["counts"]["total"], 1)
        self.assertGreaterEqual(payload["counts"]["implemented"], 1)
        self.assertEqual(len(payload["support_matrix"]), payload["counts"]["total"])
        self.assertEqual(payload["support_summary"]["total_markets"], payload["counts"]["total"])
        self.assertEqual(payload["support_summary"]["implementation"]["implemented"], 57)
        self.assertEqual(payload["support_summary"]["operations"]["copy_trading"]["guarded"], 23)
        self.assertEqual(kalshi["support"]["operations"]["paper_trading"]["status"], "supported")
        self.assertEqual(kalshi["support"]["operations"]["live_trading"]["status"], "guarded")

        support = market_support_payload(cfg, market_id="kalshi")
        self.assertEqual(support["market"]["market_id"], "kalshi")
        self.assertEqual(support["markets"], [support["market"]])
        self.assertEqual(support["support_summary"], payload["support_summary"])

    def test_market_history_payloads_serialize_normalized_records(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="space",
            display_name="Space",
            capabilities=MarketCapabilities(
                event_listing=True,
                price_reading=True,
                orderbook_reading=True,
                trade_history=True,
                candle_history=True,
            ),
        )
        adapter.list_events = lambda query, limit: [  # type: ignore[method-assign]
            MarketEvent("space", "event-1", query or "event", status="open", raw={"secret": "redact"})
        ]
        adapter.list_contracts = lambda event_id: [  # type: ignore[method-assign]
            MarketContract("space", "m:YES", event_id, "Yes", outcome="Yes", raw={"secret": "redact"})
        ]
        adapter.get_price = lambda contract_id: PriceSnapshot(  # type: ignore[method-assign]
            "space", contract_id, last=0.4, bid=0.39, ask=0.41, source="fixture", raw={"secret": "redact"}
        )
        adapter.get_orderbook = lambda contract_id: OrderBookSnapshot(  # type: ignore[method-assign]
            "space",
            contract_id,
            bids=[OrderBookLevel(0.39, 4.0)],
            asks=[OrderBookLevel(0.41, 3.0)],
            raw={"secret": "redact"},
        )
        adapter.list_trades = lambda contract_id, **_kwargs: [  # type: ignore[method-assign]
            MarketTrade("space", contract_id, "trade-1", "BUY", 0.4, 3.0, 1700000000.0)
        ]
        adapter.list_candles = lambda contract_id, **_kwargs: [  # type: ignore[method-assign]
            MarketCandle("space", contract_id, 1700000000.0, 0.35, 0.42, 0.34, 0.4, 100.0)
        ]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["space"].enabled = True
        registry = Registry()
        events = market_events_payload(cfg, registry, "space", {"query": ["launch"], "limit": ["1"]})
        contracts = market_contracts_payload(cfg, registry, "space", {"event_id": ["event-1"]})
        price = market_price_payload(cfg, registry, "space", {"contract_id": ["m:YES"]})
        orderbook = market_orderbook_payload(cfg, registry, "space", {"contract_id": ["m:YES"]})
        trades = market_trades_payload(cfg, registry, "space", {"contract_id": ["m:YES"], "limit": ["1"]})
        candles = market_candles_payload(cfg, registry, "space", {"contract_id": ["m:YES"], "resolution": ["1h"]})

        self.assertEqual(events["events"][0]["title"], "launch")
        self.assertNotIn("raw", events["events"][0])
        self.assertEqual(contracts["contracts"][0]["outcome"], "Yes")
        self.assertNotIn("raw", contracts["contracts"][0])
        self.assertEqual(price["price"]["midpoint"], 0.4)
        self.assertNotIn("raw", price["price"])
        self.assertEqual(orderbook["orderbook"]["best_ask"], 0.41)
        self.assertNotIn("raw", orderbook["orderbook"])
        self.assertEqual(trades["trades"][0]["trade_id"], "trade-1")
        self.assertEqual(trades["trades"][0]["size"], 3.0)
        self.assertEqual(candles["candles"][0]["close"], 0.4)
        self.assertEqual(candles["resolution"], "1h")

    def test_market_account_payload_requires_explicit_operation_allow_list(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="gemini_titan",
            display_name="Gemini Titan",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("positions",)  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "positions": [{"symbol": "GEMI-BTC100K26-YES"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["gemini_titan"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "gemini_titan",
            "positions",
            {"event_ticker": ["BTC100K2026"], "limit": ["10"]},
        )
        self.assertEqual(payload["operation"], "positions")
        self.assertEqual(payload["parameters"]["event_ticker"], "BTC100K2026")
        self.assertEqual(payload["data"]["positions"][0]["symbol"], "GEMI-BTC100K26-YES")
        with self.assertRaises(UnsupportedFeatureError):
            market_account_payload(cfg, Registry(), "gemini_titan", "arbitrary", {})

    def test_metaculus_account_payload_forwards_forecast_recovery_filters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="metaculus",
            display_name="Metaculus",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("forecast_posts",)  # type: ignore[attr-defined]
        calls = []

        def account_recovery(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"results": [{"id": 1101}]}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["metaculus"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "metaculus",
            "forecast_posts",
            {
                "forecaster_id": ["123"],
                "limit": ["10"],
                "offset": ["20"],
                "with_cp": ["true"],
                "include_cp_history": ["true"],
                "include_descriptions": ["true"],
            },
        )
        self.assertEqual(payload["data"]["results"][0]["id"], 1101)
        self.assertEqual(
            calls,
            [("forecast_posts", {
                "forecaster_id": "123",
                "limit": 10,
                "offset": 20,
                "with_cp": True,
                "include_cp_history": True,
                "include_descriptions": True,
            })],
        )

    def test_market_position_intent_payload_forwards_allowlisted_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="myriad_markets",
            display_name="Myriad",
            capabilities=MarketCapabilities(live_trading=True),
        )
        adapter.position_intent_operations = ("split", "neg_risk_split")  # type: ignore[attr-defined]
        calls = []

        def position_intent(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"intent_only": True}

        adapter.position_intent = position_intent  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["myriad_markets"].enabled = True
        payload = market_position_intent_payload(
            cfg,
            Registry(),
            "myriad_markets",
            {
                "operation": "neg_risk_split",
                "amount": "1000",
                "network_id": "56",
                "event_id": "0x" + "ab" * 32,
                "outcome_index": 2,
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["operation"], "neg_risk_split")
        self.assertEqual(calls, [("neg_risk_split", {
            "amount": "1000",
            "network_id": "56",
            "event_id": "0x" + "ab" * 32,
            "outcome_index": 2,
        })])
        with self.assertRaises(UnsupportedFeatureError):
            market_position_intent_payload(cfg, Registry(), "myriad_markets", {"operation": "redeem"})

    def test_ibkr_account_and_order_management_payloads_forward_fixed_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="ibkr_forecasttrader",
            display_name="IBKR ForecastTrader",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.account_recovery_operations = ("orders", "order_status")  # type: ignore[attr-defined]
        adapter.order_management_operations = ("cancel_order", "cancel_all_orders", "modify_order")  # type: ignore[attr-defined]
        account_calls = []
        order_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        def manage_orders(operation, **kwargs):
            order_calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]
        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["ibkr_forecasttrader"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "ibkr_forecasttrader",
            "orders",
            {"status": ["filled"], "force": ["true"]},
        )
        self.assertEqual(account["data"]["operation"], "orders")
        self.assertEqual(account_calls, [("orders", {"filters": "filled", "force": True})])

        instructions = {
            "conid": 721095497,
            "orderType": "LMT",
            "side": "BUY",
            "tif": "DAY",
            "quantity": 5,
            "price": 0.51,
        }
        mutation = market_order_management_payload(
            cfg,
            Registry(),
            "ibkr_forecasttrader",
            "modify_order",
            {
                "order_id": "987654",
                "instructions": instructions,
                "manual_indicator": "false",
                "external_operator": "desk-1",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(mutation["data"], {"status": "accepted"})
        self.assertEqual(order_calls[0][0], "modify_order")
        self.assertEqual(order_calls[0][1]["order_id"], "987654")
        self.assertEqual(order_calls[0][1]["instructions"], instructions)
        self.assertEqual(order_calls[0][1]["manual_indicator"], "false")
        self.assertEqual(order_calls[0][1]["external_operator"], "desk-1")
        self.assertNotIn("unexpected", order_calls[0][1])

    def test_manifold_account_and_order_management_payloads_forward_fixed_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="manifold",
            display_name="Manifold",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.account_recovery_operations = ("account", "active_orders", "order_history")  # type: ignore[attr-defined]
        adapter.order_management_operations = ("cancel_order",)  # type: ignore[attr-defined]
        account_calls = []
        order_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        def manage_orders(operation, **kwargs):
            order_calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]
        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["manifold"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "manifold",
            "active_orders",
            {
                "contract_id": ["mf-binary-1:YES"],
                "limit": ["20"],
                "before": ["bet-open-1"],
                "from": ["1760000000"],
            },
        )
        self.assertEqual(account["data"]["operation"], "active_orders")
        self.assertEqual(account_calls, [("active_orders", {
            "contract_id": "mf-binary-1:YES",
            "limit": 20,
            "before": "bet-open-1",
            "after": None,
            "before_time": None,
            "after_time": 1760000000.0,
        })])

        mutation = market_order_management_payload(
            cfg,
            Registry(),
            "manifold",
            "cancel_order",
            {
                "order_id": "bet-open-1",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(mutation["data"], {"status": "accepted"})
        self.assertEqual(order_calls, [("cancel_order", {
            "market_id": "",
            "instructions": None,
            "customer_ref": "",
            "market_version": None,
            "async_request": False,
            "confirm_global_cancel": "",
            "order_id": "bet-open-1",
            "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        })])

    def test_prophet_exchange_account_and_order_management_payloads_forward_fixed_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="prophet_exchange",
            display_name="Prophet Exchange",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.account_recovery_operations = ("balance", "transactions")  # type: ignore[attr-defined]
        adapter.order_management_operations = ("cancel_order", "cancel_orders")  # type: ignore[attr-defined]
        account_calls = []
        order_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        def manage_orders(operation, **kwargs):
            order_calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]
        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["prophet_exchange"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "prophet_exchange",
            "transactions",
            {"cursor": ["41"], "limit": ["25"]},
        )
        self.assertEqual(account["data"]["operation"], "transactions")
        self.assertEqual(account_calls, [("transactions", {"cursor": "41", "limit": 25})])

        mutation = market_order_management_payload(
            cfg,
            Registry(),
            "prophet_exchange",
            "cancel_order",
            {
                "order_id": "order-1",
                "external_id": "external-1",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(mutation["data"], {"status": "accepted"})
        self.assertEqual(order_calls, [("cancel_order", {
            "market_id": "",
            "instructions": None,
            "customer_ref": "",
            "market_version": None,
            "async_request": False,
            "confirm_global_cancel": "",
            "order_id": "order-1",
            "external_id": "external-1",
            "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        })])

        batch = market_order_management_payload(
            cfg,
            Registry(),
            "prophet_exchange",
            "cancel_orders",
            {
                "orders": [{"order_id": "order-1", "external_id": "external-1"}],
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            },
        )
        self.assertEqual(batch["data"], {"status": "accepted"})
        self.assertEqual(order_calls[-1][1]["orders"], [{"order_id": "order-1", "external_id": "external-1"}])

    def test_azuro_bet_history_account_payload_forwards_wallet_and_bounds(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="azuro",
            display_name="Azuro",
            capabilities=MarketCapabilities(),
        )
        adapter.account_recovery_operations = ("bet_history",)  # type: ignore[attr-defined]
        account_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["azuro"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "azuro",
            "bet_history",
            {
                "wallet": ["0x0000000000000000000000000000000000000001"],
                "limit": ["25"],
                "offset": ["4"],
            },
        )
        self.assertEqual(account["data"]["operation"], "bet_history")
        self.assertEqual(
            account_calls,
            [
                (
                    "bet_history",
                    {
                        "wallet": "0x0000000000000000000000000000000000000001",
                        "limit": 25,
                        "offset": 4,
                    },
                )
            ],
        )

    def test_myriad_account_activity_payload_forwards_wallet_and_bounds(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="myriad_markets",
            display_name="Myriad Markets",
            capabilities=MarketCapabilities(copy_trading=True),
        )
        adapter.account_recovery_operations = ("account_activity",)  # type: ignore[attr-defined]
        account_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["myriad_markets"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "myriad_markets",
            "account_activity",
            {
                "address": ["0x0000000000000000000000000000000000000001"],
                "limit": ["10"],
            },
        )
        self.assertEqual(account["data"]["operation"], "account_activity")
        self.assertEqual(
            account_calls,
            [
                (
                    "account_activity",
                    {
                        "wallet": "0x0000000000000000000000000000000000000001",
                        "limit": 10,
                    },
                )
            ],
        )

    def test_myriad_portfolio_payload_forwards_documented_filters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="myriad_markets",
            display_name="Myriad Markets",
            capabilities=MarketCapabilities(copy_trading=True),
        )
        adapter.account_recovery_operations = ("portfolio", "market_positions")  # type: ignore[attr-defined]
        account_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["myriad_markets"].enabled = True
        wallet = "0x0000000000000000000000000000000000000001"
        account = market_account_payload(
            cfg,
            Registry(),
            "myriad_markets",
            "portfolio",
            {
                "wallet": [wallet],
                "page": ["2"],
                "limit": ["10"],
                "trading_model": ["ob"],
                "min_shares": ["1.5"],
                "market_slug": ["btc-above-100k-2026"],
                "market_id": ["501"],
                "network_id": ["56"],
                "token_address": [wallet],
                "status": ["ongoing"],
                "keyword": ["btc"],
                "sort": ["desc"],
                "sort_by": ["profit"],
                "exclude_history": ["true"],
                "group_by_event": ["true"],
            },
        )
        self.assertEqual(account["data"]["operation"], "portfolio")
        self.assertEqual(account_calls[0], (
            "portfolio",
            {
                "wallet": wallet,
                "limit": 10,
                "page": 2,
                "trading_model": "ob",
                "min_shares": "1.5",
                "market_slug": "btc-above-100k-2026",
                "market_id": "501",
                "network_id": "56",
                "token_address": wallet,
                "status": "ongoing",
                "keyword": "btc",
                "sort": "desc",
                "sort_by": "profit",
                "exclude_history": True,
                "group_by_event": True,
            },
        ))

    def test_kalshi_account_payload_forwards_signed_read_parameters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="kalshi",
            display_name="Kalshi",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("fills",)  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "fills": [{"fill_id": "fill-1"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "kalshi",
            "fills",
            {
                "ticker": ["KXTEST-YES"],
                "order_id": ["order-1"],
                "historical": ["true"],
                "limit": ["12"],
                "from": ["1700000000"],
                "to": ["1700000100"],
                "subaccount": ["2"],
            },
        )
        self.assertEqual(payload["operation"], "fills")
        self.assertEqual(payload["parameters"]["ticker"], "KXTEST-YES")
        self.assertEqual(payload["parameters"]["order_id"], "order-1")
        self.assertTrue(payload["parameters"]["historical"])
        self.assertEqual(payload["parameters"]["limit"], 12)
        self.assertEqual(payload["parameters"]["subaccount"], 2)
        self.assertEqual(payload["data"]["fills"][0]["fill_id"], "fill-1")

    def test_polymarket_account_payload_forwards_l2_filters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="polymarket",
            display_name="Polymarket",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("active_orders", "order_detail", "fills")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "data": [{"id": "order-1"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        active = market_account_payload(
            cfg,
            Registry(),
            "polymarket",
            "active_orders",
            {"market_id": ["0x" + "b" * 64], "contract_id": ["1234567890"], "cursor": ["MTAw"]},
        )
        self.assertEqual(
            active["parameters"],
            {"market_id": "0x" + "b" * 64, "contract_id": "1234567890", "next_cursor": "MTAw"},
        )
        fills = market_account_payload(
            cfg,
            Registry(),
            "polymarket",
            "fills",
            {
                "contract_id": ["1234567890"],
                "trade_id": ["trade-1"],
                "limit": ["20"],
                "before": ["1760000300"],
                "after": ["1760000000"],
            },
        )
        self.assertEqual(fills["parameters"]["trade_id"], "trade-1")
        self.assertEqual(fills["parameters"]["limit"], 20)
        self.assertEqual(fills["parameters"]["before"], 1760000300.0)
        detail = market_account_payload(cfg, Registry(), "polymarket", "order_detail", {"order_id": ["order-1"]})
        self.assertEqual(detail["parameters"], {"order_id": "order-1"})

    def test_polymarket_order_management_payload_forwards_cancel_fields_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="polymarket",
            display_name="Polymarket",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_orders", "cancel_all_orders", "cancel_market_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "polymarket",
            "cancel_market_orders",
            {
                "market_id": "0x" + "b" * 64,
                "asset_id": "1234567890",
                "contract_id": "1234567890",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["market_id"], "0x" + "b" * 64)
        self.assertEqual(calls[0][1]["asset_id"], "1234567890")
        self.assertEqual(calls[0][1]["contract_id"], "1234567890")
        self.assertNotIn("unexpected", calls[0][1])

    def test_gemini_order_management_payload_forwards_only_documented_cancel_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="gemini_titan",
            display_name="Gemini Predictions",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "batch_cancel_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["gemini_titan"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "gemini_titan",
            "batch_cancel_orders",
            {
                "orders": [106817811, "106817812"],
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["operation"], "batch_cancel_orders")
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["orders"], [106817811, "106817812"])
        self.assertEqual(
            calls[0][1]["confirm_order_management"],
            "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )
        self.assertNotIn("unexpected", calls[0][1])

    def test_hyperliquid_account_payload_forwards_safe_dex_and_limit(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="hyperliquid",
            display_name="Hyperliquid",
            capabilities=MarketCapabilities(credentials_required=False),
        )
        adapter.account_recovery_operations = ("active_orders", "order_history")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "orders": [{"coin": "#10"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["hyperliquid"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "hyperliquid",
            "active_orders",
            {"dex": ["xyz"]},
        )
        self.assertEqual(payload["operation"], "active_orders")
        self.assertEqual(payload["parameters"], {"dex": "xyz"})
        self.assertEqual(payload["data"]["orders"][0]["coin"], "#10")
        history = market_account_payload(cfg, Registry(), "hyperliquid", "order_history", {"limit": ["12"]})
        self.assertEqual(
            history["parameters"],
            {"limit": 12, "status": "filled", "from_timestamp": None, "to_timestamp": None},
        )

    def test_opinion_account_payload_forwards_page_filters_and_path_safe_order_id(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="opinion_labs",
            display_name="Opinion Labs",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("order_history", "order_detail", "positions")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "result": {"list": [{"orderId": "order-1"}]},
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["opinion_labs"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "opinion_labs",
            "order_history",
            {
                "page": ["2"],
                "limit": ["20"],
                "market_id": ["77"],
                "chain_id": ["56"],
                "status": ["1,2"],
            },
        )
        self.assertEqual(
            payload["parameters"],
            {"page": 2, "limit": 20, "market_id": "77", "chain_id": "56", "status": "1,2"},
        )
        detail = market_account_payload(
            cfg, Registry(), "opinion_labs", "order_detail", {"order_id": ["order-1"]}
        )
        self.assertEqual(detail["parameters"], {"order_id": "order-1"})
        clamped = market_account_payload(cfg, Registry(), "opinion_labs", "order_history", {"limit": ["99"]})
        self.assertEqual(clamped["parameters"]["limit"], 20)

    def test_betfair_account_payload_forwards_cleared_order_filters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="betfair_exchange",
            display_name="Betfair Exchange",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = (  # type: ignore[attr-defined]
            "active_orders",
            "cleared_orders",
            "funds",
            "account",
            "statement",
            "currency_rates",
        )
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "clearedOrders": [{"betId": "bet-1"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["betfair_exchange"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "betfair_exchange",
            "cleared_orders",
            {
                "contract_id": ["1.234:101"],
                "status": ["SETTLED"],
                "limit": ["12"],
                "offset": ["2"],
                "group_by": ["RUNNER"],
                "include_item_description": ["true"],
            },
        )
        self.assertEqual(
            payload["parameters"],
            {
                "bet_status": "SETTLED",
                "market_id": "1.234",
                "event_type_id": "",
                "event_id": "",
                "runner_id": "101",
                "bet_id": "",
                "group_by": "RUNNER",
                "include_item_description": True,
                "limit": 12,
                "offset": 2,
                "from_timestamp": None,
                "to_timestamp": None,
            },
        )
        active = market_account_payload(
            cfg,
            Registry(),
            "betfair_exchange",
            "active_orders",
            {
                "contract_id": ["1.234:101"],
                "status": ["EXECUTABLE"],
                "order_by": ["BY_PLACE_TIME"],
                "sort_dir": ["LATEST_TO_EARLIEST"],
                "limit": ["8"],
                "offset": ["3"],
            },
        )
        self.assertEqual(
            active["parameters"],
            {
                "market_id": "1.234",
                "contract_id": "1.234:101",
                "status": "EXECUTABLE",
                "order_by": "BY_PLACE_TIME",
                "sort_dir": "LATEST_TO_EARLIEST",
                "include_item_description": False,
                "limit": 8,
                "offset": 3,
                "from_timestamp": None,
                "to_timestamp": None,
            },
        )
        funds = market_account_payload(cfg, Registry(), "betfair_exchange", "funds", {"wallet": ["UK"]})
        self.assertEqual(funds["parameters"], {"wallet": "UK"})
        account = market_account_payload(cfg, Registry(), "betfair_exchange", "account", {})
        self.assertEqual(account["parameters"], {})
        statement = market_account_payload(
            cfg,
            Registry(),
            "betfair_exchange",
            "statement",
            {
                "locale": ["en"],
                "wallet": ["UK"],
                "limit": ["12"],
                "offset": ["4"],
                "from": ["1780272000"],
                "to": ["1780358400"],
            },
        )
        self.assertEqual(
            statement["parameters"],
            {
                "locale": "en",
                "limit": 12,
                "offset": 4,
                "include_item": True,
                "wallet": "UK",
                "from_timestamp": 1780272000.0,
                "to_timestamp": 1780358400.0,
            },
        )
        rates = market_account_payload(
            cfg,
            Registry(),
            "betfair_exchange",
            "currency_rates",
            {"from_currency": ["GBP"]},
        )
        self.assertEqual(rates["parameters"], {"from_currency": "GBP"})

    def test_betfair_order_management_payload_forwards_only_allowlisted_mutation_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="betfair_exchange",
            display_name="Betfair Exchange",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.order_management_operations = ("cancel_orders", "update_orders", "replace_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "SUCCESS"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["betfair_exchange"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "betfair_exchange",
            "cancel_orders",
            {
                "exchange_market_id": "1.234",
                "instructions": [{"bet_id": "bet-1", "size_reduction": 1.25}],
                "customerRef": "cancel-1",
                "async": False,
                "confirm_global_cancel": "",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["operation"], "cancel_orders")
        self.assertEqual(payload["parameters"]["market_id"], "1.234")
        self.assertEqual(payload["parameters"]["customer_ref"], "cancel-1")
        self.assertEqual(payload["data"], {"status": "SUCCESS"})
        self.assertEqual(
            calls,
            [
                (
                    "cancel_orders",
                    {
                        "market_id": "1.234",
                        "instructions": [{"bet_id": "bet-1", "size_reduction": 1.25}],
                        "customer_ref": "cancel-1",
                        "market_version": None,
                        "async_request": False,
                        "confirm_global_cancel": "",
                    },
                )
            ],
        )
        with self.assertRaises(UnsupportedFeatureError):
            market_order_management_payload(cfg, Registry(), "betfair_exchange", "place_orders", {})

    def test_kalshi_order_management_payload_forwards_documented_fields_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="kalshi",
            display_name="Kalshi",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "batch_cancel_orders", "amend_order", "decrease_order")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "kalshi",
            "amend_order",
            {
                "order_id": "order-1",
                "ticker": "KXTEST-1",
                "side": "bid",
                "price": "0.44",
                "count": "5",
                "subaccount": 1,
                "exchange_index": 0,
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["operation"], "amend_order")
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["order_id"], "order-1")
        self.assertEqual(calls[0][1]["ticker"], "KXTEST-1")
        self.assertEqual(calls[0][1]["confirm_order_management"], "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS")
        self.assertNotIn("unexpected", calls[0][1])

    def test_hyperliquid_order_management_payload_forwards_signed_action_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="hyperliquid",
            display_name="Hyperliquid",
            capabilities=MarketCapabilities(credentials_required=False, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "schedule_cancel")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["hyperliquid"].enabled = True
        signed = {
            "action": {"type": "cancel", "cancels": [{"a": 100000000, "o": 123456789}]},
            "nonce": 1700000000000,
            "signature": "0x" + "ab" * 65,
        }
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "hyperliquid",
            "cancel_order",
            {
                "signed_action": signed,
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["signed_action"], signed)
        self.assertEqual(calls[0][1]["confirm_order_management"], "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS")
        self.assertNotIn("unexpected", calls[0][1])
        self.assertIsNone(calls[0][1]["instructions"])

    def test_smarkets_account_and_order_management_payloads_forward_bounded_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="smarkets",
            display_name="Smarkets",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.account_recovery_operations = ("order_history", "account")  # type: ignore[attr-defined]
        adapter.order_management_operations = ("cancel_order", "cancel_orders")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
        }
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["smarkets"].enabled = True
        account = market_account_payload(
            cfg,
            Registry(),
            "smarkets",
            "order_history",
            {"status": ["created,filled"], "limit": ["24"], "unexpected": ["ignored"]},
        )
        self.assertEqual(account["parameters"], {"status": "created,filled", "limit": 24})
        mutation = market_order_management_payload(
            cfg,
            Registry(),
            "smarkets",
            "cancel_orders",
            {
                "market_id": "market-1",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(mutation["data"], {"status": "accepted"})
        self.assertEqual(
            calls,
            [
                (
                    "cancel_orders",
                    {
                        "market_id": "market-1",
                        "instructions": None,
                        "customer_ref": "",
                        "market_version": None,
                        "async_request": False,
                        "confirm_global_cancel": "",
                        "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                    },
                )
            ],
        )

    def test_matchbook_account_payload_forwards_report_and_offer_filters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="matchbook",
            display_name="Matchbook",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("settled_bets", "current_bets", "current_offers", "balance", "account")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "data": {"operation": operation},
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["matchbook"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "matchbook",
            "settled_bets",
            {
                "sport_id": ["1"],
                "event_id": ["101"],
                "market_id": ["202"],
                "limit": ["12"],
                "offset": ["2"],
                "from": ["1780344000"],
                "to": ["1780347600"],
                "odds_type": ["DECIMAL"],
            },
        )
        self.assertEqual(
            payload["parameters"],
            {
                "offset": 2,
                "limit": 12,
                "sport_id": "1",
                "event_id": "101",
                "market_id": "202",
                "odds_type": "DECIMAL",
                "from_timestamp": 1780344000.0,
                "to_timestamp": 1780347600.0,
            },
        )
        offers = market_account_payload(
            cfg,
            Registry(),
            "matchbook",
            "current_offers",
            {
                "side": ["back"],
                "offer_status": ["open,matched"],
                "interval": ["30"],
                "include_edits": ["true"],
                "aggregation_type": ["average"],
            },
        )
        self.assertEqual(offers["parameters"]["side"], "back")
        self.assertEqual(offers["parameters"]["status"], "open,matched")
        self.assertEqual(offers["parameters"]["interval"], 30)
        self.assertTrue(offers["parameters"]["include_edits"])
        self.assertEqual(offers["parameters"]["aggregation_type"], "average")

    def test_matchbook_order_management_payload_forwards_only_documented_fields(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="matchbook",
            display_name="Matchbook",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_offers", "edit_offer")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["matchbook"].enabled = True
        batch = market_order_management_payload(
            cfg,
            Registry(),
            "matchbook",
            "cancel_offers",
            {
                "offer_ids": [404, 405],
                "event_ids": "101",
                "market_ids": "202",
                "runner_ids": "303",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(batch["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["offer_ids"], [404, 405])
        self.assertEqual(calls[0][1]["event_ids"], "101")
        self.assertNotIn("unexpected", calls[0][1])

        single = market_order_management_payload(
            cfg,
            Registry(),
            "matchbook",
            "edit_offer",
            {
                "offer_id": 404,
                "current_odds": 1.5,
                "new_odds": 2.0,
                "current_stake": 5,
                "new_stake": 6,
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
            },
        )
        self.assertEqual(single["operation"], "edit_offer")
        self.assertEqual(calls[1][1]["offer_id"], 404)
        self.assertEqual(calls[1][1]["new_stake"], 6)

    def test_myriad_order_management_payload_forwards_signed_fields_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="myriad_markets",
            display_name="Myriad",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "batch_cancel_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["myriad_markets"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "myriad_markets",
            "cancel_order",
            {
                "order_hash": "0x" + "12" * 32,
                "trader": "0x1234567890123456789012345678901234567890",
                "timestamp": 1719835200,
                "signature": "0x" + "ab" * 65,
                "network_id": 56,
                "allow_partial": True,
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["order_hash"], "0x" + "12" * 32)
        self.assertEqual(calls[0][1]["network_id"], 56)
        self.assertNotIn("unexpected", calls[0][1])

    def test_opinion_order_management_payload_forwards_sdk_filters_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="opinion_labs",
            display_name="Opinion Labs",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "batch_cancel_orders", "cancel_all_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["opinion_labs"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "opinion_labs",
            "cancel_all_orders",
            {
                "market_id": "77",
                "side": "BUY",
                "confirm_global_cancel": "CANCEL ALL OPINION ORDERS",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["market_id"], "77")
        self.assertEqual(calls[0][1]["side"], "BUY")
        self.assertEqual(calls[0][1]["confirm_global_cancel"], "CANCEL ALL OPINION ORDERS")
        self.assertNotIn("unexpected", calls[0][1])

    def test_limitless_order_management_payload_forwards_fixed_cancellation_fields_only(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="limitless_exchange",
            display_name="Limitless Exchange",
            capabilities=MarketCapabilities(credentials_required=True, live_trading=True),
        )
        adapter.order_management_operations = ("cancel_order", "batch_cancel_orders", "cancel_all_orders")  # type: ignore[attr-defined]
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"status": "accepted"}

        adapter.manage_orders = manage_orders  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["limitless_exchange"].enabled = True
        payload = market_order_management_payload(
            cfg,
            Registry(),
            "limitless_exchange",
            "cancel_all_orders",
            {
                "market_slug": "doge-above-021652-sep-1-1200-utc",
                "confirm_global_cancel": "CANCEL ALL LIMITLESS ORDERS",
                "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                "unexpected": "ignored",
            },
        )
        self.assertEqual(payload["data"], {"status": "accepted"})
        self.assertEqual(calls[0][1]["market_slug"], "doge-above-021652-sep-1-1200-utc")
        self.assertEqual(calls[0][1]["confirm_global_cancel"], "CANCEL ALL LIMITLESS ORDERS")
        self.assertEqual(calls[0][1]["confirm_order_management"], "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS")
        self.assertNotIn("unexpected", calls[0][1])

    def test_limitless_account_payload_forwards_delegated_read_parameters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="limitless_exchange",
            display_name="Limitless Exchange",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("user_orders",)  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "orders": [{"order_id": "order-1"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["limitless_exchange"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "limitless_exchange",
            "user_orders",
            {
                "market_slug": ["doge-above-021652-sep-1-1200-utc"],
                "on_behalf_of": ["profile-123"],
            },
        )
        self.assertEqual(payload["operation"], "user_orders")
        self.assertEqual(
            payload["parameters"],
            {
                "on_behalf_of": "profile-123",
                "market_slug": "doge-above-021652-sep-1-1200-utc",
            },
        )
        self.assertEqual(payload["data"]["orders"][0]["order_id"], "order-1")

    def test_xmarket_account_payload_forwards_bounded_market_order_parameters(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="xmarket",
            display_name="Xmarket",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = ("positions", "user_orders", "market_orders")  # type: ignore[attr-defined]
        adapter.account_recovery = lambda operation, **kwargs: {  # type: ignore[method-assign]
            "operation": operation,
            "parameters": kwargs,
            "items": [{"id": "xorder-1"}],
        }

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["xmarket"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "xmarket",
            "market_orders",
            {
                "market_id": ["market-1"],
                "status": ["open"],
                "page": ["2"],
                "limit": ["25"],
                "unexpected": ["ignored"],
            },
        )
        self.assertEqual(payload["operation"], "market_orders")
        self.assertEqual(
            payload["parameters"],
            {"status": "open", "page": 2, "limit": 25, "market_id": "market-1"},
        )
        self.assertEqual(payload["data"]["items"][0]["id"], "xorder-1")

    def test_xo_account_payload_forwards_documented_filters_and_path_ids(self) -> None:
        adapter = MarketAdapter({})
        adapter.metadata = MarketMetadata(
            market_id="xo_market",
            display_name="XO Market",
            capabilities=MarketCapabilities(credentials_required=True),
        )
        adapter.account_recovery_operations = (
            "account",
            "positions",
            "orders",
            "trades",
            "settlement",
            "settlement_history",
            "audit_logs",
        )  # type: ignore[attr-defined]
        calls = []

        def account_recovery(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        adapter.account_recovery = account_recovery  # type: ignore[method-assign]

        class Registry:
            def create(self, _market_id: str, _settings=None):
                return adapter

        cfg = AppConfig()
        cfg.markets["xo_market"].enabled = True
        payload = market_account_payload(
            cfg,
            Registry(),
            "xo_market",
            "trades",
            {
                "market_id": ["us-election-2028"],
                "outcome_id": ["vance"],
                "from": ["2024-12-01T09:15:00Z"],
                "to": ["2024-12-01T09:30:00Z"],
                "limit": ["12"],
                "unexpected": ["ignored"],
            },
        )
        self.assertEqual(payload["parameters"]["limit"], 12)
        self.assertEqual(payload["parameters"]["market_id"], "us-election-2028")
        self.assertEqual(payload["parameters"]["outcome_id"], "vance")
        self.assertEqual(payload["parameters"]["start_time"], "2024-12-01T09:15:00Z")
        self.assertEqual(payload["parameters"]["end_time"], "2024-12-01T09:30:00Z")

        settlement = market_account_payload(
            cfg,
            Registry(),
            "xo_market",
            "settlement_history",
            {"contract_id": ["us-election-2028:vance"], "limit": ["5"], "cursor": ["next"]},
        )
        self.assertEqual(
            settlement["parameters"],
            {"market_id": "us-election-2028", "limit": 5, "cursor": "next"},
        )
        self.assertEqual(calls[0][0], "trades")
        self.assertEqual(calls[1][0], "settlement_history")

    def test_markets_payload_includes_diagnostics_without_secret_values(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        cfg.markets["kalshi"].settings.update(
            {
                "credential_env_vars": ["KALSHI_API_KEY_ID"],
                "kalshi_api_key_id": "super-secret-key",
                "kalshi_private_key_path": "C:/secret/private.pem",
                "nested": {"api_token": "nested-secret-token", "public": "ok"},
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": 9,
            }
        )

        payload = markets_payload(cfg)

        kalshi = next(market for market in payload["markets"] if market["market_id"] == "kalshi")
        self.assertEqual(kalshi["settings"]["kalshi_api_key_id"], "***")
        self.assertEqual(kalshi["settings"]["kalshi_private_key_path"], "***")
        self.assertEqual(kalshi["settings"]["nested"]["api_token"], "***")
        self.assertEqual(kalshi["settings"]["nested"]["public"], "ok")
        self.assertEqual(kalshi["credential_env_vars"], ["KALSHI_API_KEY_ID"])
        self.assertIn({"name": "KALSHI_API_KEY_ID", "source": "config:kalshi_api_key_id"}, kalshi["credential_sources"])
        self.assertIn(
            {"name": "KALSHI_PRIVATE_KEY_PATH", "source": "config:kalshi_private_key_path"},
            kalshi["credential_sources"],
        )
        self.assertTrue(kalshi["safety"]["live_trading_enabled"])
        self.assertTrue(kalshi["safety"]["live_trading_confirmed"])
        self.assertEqual(kalshi["safety"]["live_trading_max_size"], 9)
        self.assertIn("live armed", kalshi["status_text"])
        rendered = json.dumps(kalshi)
        self.assertNotIn("super-secret-key", rendered)
        self.assertNotIn("private.pem", rendered)
        self.assertNotIn("nested-secret-token", rendered)

    def test_market_health_failure_does_not_leak_raw_exception_text(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True

        payload = markets_payload(cfg, SecretFailRegistry())

        rendered = json.dumps(payload)
        kalshi = next(market for market in payload["markets"] if market["market_id"] == "kalshi")
        self.assertFalse(kalshi["health"]["ok"])
        self.assertEqual(kalshi["health"]["message"], "Adapter health check failed.")
        self.assertEqual(kalshi["health"]["error_type"], "RuntimeError")
        self.assertNotIn("super-secret-token", rendered)

    def test_paper_payload_exposes_history_and_aggregated_positions(self) -> None:
        cfg = AppConfig()
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            ),
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="SELL",
                size=0.5,
                limit_price=0.60,
                accepted=True,
                message="accepted",
            ),
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="REJECTED",
                side="BUY",
                size=1,
                limit_price=0.20,
                accepted=False,
                message="rejected",
            ),
        ]

        payload = paper_payload(cfg)

        self.assertEqual(payload["counts"]["history"], 3)
        self.assertEqual(payload["counts"]["accepted"], 2)
        self.assertEqual(payload["counts"]["rejected"], 1)
        self.assertEqual(len(payload["positions"]), 1)
        position = payload["positions"][0]
        self.assertEqual(position["market_id"], "kalshi")
        self.assertEqual(position["contract_id"], "KALSHI-CONTRACT")
        self.assertAlmostEqual(position["net_size"], 1.5)
        self.assertAlmostEqual(position["notional"], 0.58)
        self.assertEqual(payload["summary"]["positions"], 1)

    def test_paper_quote_and_side_aware_limit_use_adapter_data(self) -> None:
        adapter = FakePaperAdapter()
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        payload = {"market_id": "kalshi", "contract_id": "KALSHI-CONTRACT", "side": "BUY"}

        quote = paper_quote_payload(cfg, FakeRegistry(adapter), payload)
        limit = paper_quote_limit_payload(cfg, FakeRegistry(adapter), payload)

        self.assertEqual(adapter.prices, ["KALSHI-CONTRACT", "KALSHI-CONTRACT"])
        self.assertEqual(adapter.orderbooks, ["KALSHI-CONTRACT", "KALSHI-CONTRACT"])
        self.assertEqual(quote["price"]["last"], 0.62)
        self.assertEqual(quote["best_bid"], 0.58)
        self.assertEqual(quote["best_ask"], 0.66)
        self.assertEqual(limit["limit_price"], 0.66)
        self.assertEqual(limit["source"], "best_ask")

    def test_paper_order_impact_and_submit_record_history(self) -> None:
        adapter = FakePaperAdapter()
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            )
        ]
        order = paper_order_from_payload(
            {
                "market_id": "kalshi",
                "contract_id": "KALSHI-CONTRACT",
                "side": "SELL",
                "size": 0.5,
                "limit_price": 0.60,
            }
        )

        impact = paper_order_impact(cfg.paper_trades, order)
        result = submit_paper_order(cfg, FakeRegistry(adapter), order.__dict__)

        self.assertEqual(impact["effect"], "reduces position")
        self.assertEqual(impact["projected_net"], 1.5)
        self.assertEqual(len(adapter.orders), 1)
        self.assertEqual(len(cfg.paper_trades), 2)
        self.assertTrue(result["record"]["accepted"])
        self.assertEqual(result["record"]["average_price"], 0.60)

    def test_history_and_position_refill_payloads_return_order_form_values(self) -> None:
        cfg = AppConfig()
        record = PaperTradeRecord(
            market_id="kalshi",
            contract_id="KALSHI-CONTRACT",
            side="BUY",
            size=2,
            limit_price=0.44,
            accepted=True,
            message="accepted",
        )
        cfg.paper_trades = [record]

        history = history_refill_payload(cfg, record.id)
        position = position_refill_payload(cfg, "kalshi", "KALSHI-CONTRACT")

        self.assertEqual(history["side"], "BUY")
        self.assertEqual(history["limit_price"], 0.44)
        self.assertEqual(position["side"], "SELL")
        self.assertEqual(position["size"], 2)
        self.assertIsNone(position["limit_price"])

    def test_refresh_selected_paper_mark_and_payload_unrealized(self) -> None:
        adapter = FakePaperAdapter()
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            )
        ]

        marks = refresh_selected_paper_mark(cfg, FakeRegistry(adapter), "kalshi", "KALSHI-CONTRACT", {})
        payload = paper_payload(cfg, marks)

        self.assertEqual(adapter.prices, ["KALSHI-CONTRACT"])
        self.assertEqual(marks[("kalshi", "KALSHI-CONTRACT")]["mark_price"], 0.60)
        self.assertEqual(payload["positions"][0]["mark_source"], "bid")
        self.assertAlmostEqual(payload["positions"][0]["unrealized"], 0.32)
        self.assertEqual(payload["summary"]["marked"], 1)

    def test_alert_payload_create_update_and_delete(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        adapter = FakePaperAdapter()

        alert = alert_from_payload(
            cfg,
            FakeRegistry(adapter),
            {
                "market_id": "kalshi",
                "contract_id": "KALSHI-CONTRACT",
                "label": "Kalshi alert",
                "direction": "above",
                "threshold": 0.65,
                "source": "midpoint",
                "once": False,
            },
        )
        cfg.alerts.append(alert)

        payload = alerts_payload(cfg, FakeRegistry(adapter), {})
        self.assertEqual(payload["counts"]["total"], 1)
        self.assertEqual(payload["counts"]["enabled"], 1)
        self.assertEqual(payload["alerts"][0]["status"]["label"], "waiting for midpoint")
        self.assertEqual(payload["alerts"][0]["contract_id"], "KALSHI-CONTRACT")

        alert_from_payload(
            cfg,
            FakeRegistry(adapter),
            {
                "market_id": "kalshi",
                "contract_id": "KALSHI-CONTRACT",
                "threshold": 0.5,
                "source": "best_bid",
                "enabled": False,
            },
            existing=alert,
        )
        self.assertFalse(alert.enabled)
        self.assertEqual(alert.source, "best_bid")
        deleted = delete_alert(cfg, alert.id)
        self.assertEqual(deleted.id, alert.id)
        self.assertEqual(alerts_payload(cfg, FakeRegistry(adapter))["counts"]["total"], 0)

    def test_refresh_alert_price_updates_current_state_and_triggers_once_alert(self) -> None:
        cfg = AppConfig()
        cfg.markets["kalshi"].enabled = True
        adapter = FakePaperAdapter()
        alert = PriceAlert(
            market_id="kalshi",
            token_id="KALSHI-CONTRACT",
            label="Kalshi last trade",
            direction="above",
            threshold=0.60,
            source="last_trade",
            once=True,
        )
        cfg.alerts.append(alert)
        price_state = {}

        result = refresh_alert_price(cfg, FakeRegistry(adapter), alert, price_state)
        payload = alerts_payload(cfg, FakeRegistry(adapter), price_state)

        self.assertEqual(adapter.prices, ["KALSHI-CONTRACT"])
        self.assertEqual(result["values"]["last_trade"], 0.62)
        self.assertTrue(alert.triggered)
        self.assertFalse(alert.enabled)
        self.assertEqual(alert.last_value, 0.62)
        self.assertEqual(payload["alerts"][0]["current_value"], 0.62)
        self.assertEqual(payload["alerts"][0]["status"]["label"], "triggered/disabled")

    def test_refresh_polymarket_alert_uses_last_trade_price(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        adapter = FakePolymarketAdapter()
        alert = PriceAlert(
            market_id="polymarket",
            token_id="token-yes",
            label="Polymarket last trade",
            direction="above",
            threshold=0.60,
            source="last_trade",
            once=True,
        )
        cfg.alerts.append(alert)
        price_state = {}

        result = refresh_alert_price(cfg, FakeRegistry(adapter), alert, price_state)

        self.assertEqual(adapter.prices, ["token-yes"])
        self.assertEqual(result["values"]["last_trade"], 0.61)
        self.assertTrue(alert.triggered)
        self.assertFalse(alert.enabled)

    def test_wallet_payload_add_update_delete(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True

        wallet = add_wallet_watch(cfg, {"wallet": WALLET.upper().replace("X", "x", 1), "display_name": "tracked"})
        payload = wallets_payload(cfg)

        self.assertEqual(payload["counts"]["total"], 1)
        self.assertEqual(payload["wallets"][0]["wallet"], WALLET)
        self.assertEqual(payload["wallets"][0]["display_name"], "tracked")

        update_wallet_watch(cfg, wallet.id, {"wallet": WALLET, "display_name": "renamed", "enabled": False, "only_market_slug": "slug"})
        self.assertFalse(cfg.wallets[0].enabled)
        self.assertEqual(cfg.wallets[0].only_market_slug, "slug")
        deleted = delete_wallet_watch(cfg, wallet.id)

        self.assertEqual(deleted.wallet, WALLET)
        self.assertEqual(wallets_payload(cfg)["counts"]["total"], 0)

    def test_poll_wallet_activity_updates_seen_state_and_copy_simulation_preview(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        cfg.wallets = [WalletWatch(wallet=WALLET, display_name="tracked")]
        cfg.copytrading = CopyTradeSettings(
            enabled=True,
            live=False,
            follow_wallet=WALLET,
            follow_wallets=[WALLET],
            scale=1.0,
            max_usdc_per_trade=1.0,
            slippage=0.02,
        )
        activity = [
            {
                "transactionHash": "tx2",
                "timestamp": 101,
                "proxyWallet": WALLET,
                "asset": "token-yes",
                "side": "BUY",
                "price": "0.44",
                "size": "10",
                "slug": "market",
                "outcome": "Yes",
            },
            {
                "transactionHash": "tx1",
                "timestamp": 100,
                "proxyWallet": WALLET,
                "asset": "token-yes",
                "side": "BUY",
                "price": "0.43",
                "size": "3",
                "slug": "market",
                "outcome": "Yes",
            },
        ]
        recent: list[dict] = []

        with patch("web_api.data_api.get_activity", return_value=activity):
            result = poll_wallet_activity(cfg, FakeRegistry(FakePolymarketAdapter()), recent)

        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["activity"]), 2)
        self.assertEqual(cfg.wallets[0].last_seen_ts, 101)
        self.assertEqual(set(cfg.wallets[0].seen_activity_keys), {"tx:tx1", "tx:tx2"})
        newest = result["activity"][0]
        self.assertEqual(newest["transaction_hash"], "tx2")
        preview = newest["copy_preview"]
        self.assertEqual(preview["status"], "simulation")
        self.assertFalse(preview["live"])
        self.assertTrue(preview["pricing"]["capped_by_max_usdc"])
        self.assertAlmostEqual(preview["order"]["limit_price"], 0.47)

    def test_opinion_wallet_activity_uses_official_feed_and_copy_simulation(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "opinion_labs"
        cfg.markets["opinion_labs"].enabled = True
        cfg.wallets = [WalletWatch(wallet=WALLET, display_name="tracked")]
        cfg.copytrading = CopyTradeSettings(
            enabled=True,
            live=False,
            follow_wallet=WALLET,
            follow_wallets=[WALLET],
            scale=1.0,
            max_usdc_per_trade=1.0,
            slippage=0.02,
        )
        recent: list[dict] = []

        result = poll_wallet_activity(cfg, FakeRegistry(FakeOpinionCopyAdapter()), recent)

        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["activity"]), 1)
        preview = result["activity"][0]["copy_preview"]
        self.assertEqual(preview["status"], "simulation")
        self.assertEqual(preview["order"]["market_id"], "opinion_labs")
        self.assertEqual(preview["order"]["contract_id"], "77:YES:0xyes")
        self.assertTrue(preview["pricing"]["capped_by_max_usdc"])

    def test_myriad_wallet_activity_uses_public_feed_and_collateral_budget_copy(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "myriad_markets"
        cfg.markets["myriad_markets"].enabled = True
        cfg.wallets = [WalletWatch(wallet=WALLET, display_name="tracked")]
        cfg.copytrading = CopyTradeSettings(
            enabled=True,
            live=False,
            follow_wallet=WALLET,
            follow_wallets=[WALLET],
            scale=1.0,
            max_usdc_per_trade=1.0,
            slippage=0.02,
        )
        recent: list[dict] = []

        result = poll_wallet_activity(cfg, FakeRegistry(FakeMyriadCopyAdapter()), recent)

        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["activity"]), 1)
        preview = result["activity"][0]["copy_preview"]
        self.assertEqual(preview["status"], "simulation")
        self.assertEqual(preview["order"]["market_id"], "myriad_markets")
        self.assertEqual(preview["order"]["contract_id"], "501:1")
        self.assertAlmostEqual(preview["order"]["size"], 1.0)
        self.assertAlmostEqual(preview["order"]["approx_notional"], 1.0)
        self.assertTrue(preview["pricing"]["capped_by_max_usdc"])

    def test_azuro_wallet_copy_uses_decimal_odds_and_stake_budget(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "azuro"
        cfg.markets["azuro"].enabled = True
        cfg.wallets = [WalletWatch(wallet=WALLET, display_name="tracked")]
        cfg.copytrading = CopyTradeSettings(
            enabled=True,
            live=False,
            follow_wallet=WALLET,
            follow_wallets=[WALLET],
            scale=1.0,
            max_usdc_per_trade=1.0,
            slippage=0.02,
        )

        preview = copy_trade_preview_from_activity(
            cfg,
            FakeRegistry(FakeAzuroCopyAdapter()),
            {
                "proxyWallet": WALLET,
                "asset": "30061006000000000029214016:300610060000000000649714110000000000000227249395:29",
                "side": "BUY",
                "price": 1 / 1.85,
                "odds": 1.85,
                "size": 10.0,
                "transactionHash": "azuro-tx-1",
            },
        )

        self.assertEqual(preview["status"], "simulation")
        self.assertAlmostEqual(preview["order"]["size"], 1.0)
        self.assertAlmostEqual(preview["order"]["limit_price"], 1.85 * 0.98)
        self.assertAlmostEqual(preview["order"]["approx_notional"], 1.0)
        self.assertEqual(preview["pricing"]["raw_odds"], 1.85)
        self.assertTrue(preview["pricing"]["capped_by_max_usdc"])

    def test_manifold_wallet_and_copy_settings_use_prefixed_public_identity(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "manifold"
        cfg.markets["manifold"].enabled = True

        wallet = add_wallet_watch(cfg, {"wallet": "Manifold:ForecastUser", "display_name": "ForecastUser"})
        settings = apply_copy_settings_patch(
            cfg,
            {
                "enabled": True,
                "follow_wallets": ["MANIFOLD:ForecastUser"],
                "copy_percentage": 100,
                "max_usdc_per_trade": 5,
                "slippage": 0.01,
            },
        )
        payload = copy_payload(cfg, FakeRegistry(FakePolymarketAdapter()))

        self.assertEqual(wallet.wallet, "manifold:forecastuser")
        self.assertEqual(settings.normalized_follow_wallets(), ["manifold:forecastuser"])
        self.assertTrue(payload["copy_trading_supported"])
        self.assertEqual(payload["activity_identity_hint"], "manifold:<username>")

        with self.assertRaises(ValueError):
            add_wallet_watch(cfg, {"wallet": "ForecastUser"})

    def test_copy_settings_and_live_preview_use_shared_preflight_without_ordering(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        cfg.markets["polymarket"].settings.update(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": 10,
                "live_trading_max_notional": 5,
            }
        )
        settings = apply_copy_settings_patch(
            cfg,
            {
                "enabled": True,
                "live": True,
                "follow_wallet": WALLET,
                "follow_wallets": [WALLET],
                "copy_percentage": 100,
                "max_usdc_per_trade": 2,
                "slippage": 0.01,
                "allow_sells": True,
            },
        )

        payload = copy_preview_payload(
            cfg,
            FakeRegistry(FakePolymarketAdapter()),
            {"proxyWallet": WALLET, "asset": "token-yes", "side": "BUY", "size": 2, "price": 0.44},
        )
        copy_state = copy_payload(cfg, FakeRegistry(FakePolymarketAdapter()))

        self.assertTrue(settings.live)
        self.assertEqual(copy_state["status"], "live requested")
        self.assertEqual(payload["preview"]["status"], "live_preflight")
        self.assertFalse(payload["preview"]["blocked"])
        self.assertEqual(payload["preview"]["preflight"]["feature"], "live copy trading")
        self.assertEqual(payload["preview"]["preflight"]["metadata_keys"], ["activity_key", "source", "tif"])

    def test_copy_settings_accept_zero_to_one_hundred_percent(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True

        settings = apply_copy_settings_patch(
            cfg,
            {
                "enabled": True,
                "follow_wallets": [WALLET, WALLET_2],
                "copy_percentage": 0,
                "max_usdc_per_trade": 2,
                "slippage": 0.01,
            },
        )

        self.assertEqual(settings.scale, 0.0)
        self.assertEqual(settings.normalized_follow_wallets(), [WALLET, WALLET_2])
        self.assertEqual(settings.to_dict()["copy_percentage"], 0.0)
        with self.assertRaises(ValueError):
            apply_copy_settings_patch(cfg, {"copy_percentage": 101})

    def test_copy_preview_supports_multiple_follow_wallets_and_conflict_guard(self) -> None:
        cfg = AppConfig()
        cfg.markets["polymarket"].enabled = True
        cfg.copytrading = CopyTradeSettings(
            enabled=True,
            live=False,
            follow_wallet=WALLET,
            follow_wallets=[WALLET, WALLET_2],
            scale=1.0,
            max_usdc_per_trade=10.0,
            slippage=0.01,
            conflict_guard=True,
        )
        conflict_state: dict[str, dict] = {}
        first = {
            "transactionHash": "tx1",
            "timestamp": 100,
            "proxyWallet": WALLET,
            "asset": "token-yes",
            "side": "BUY",
            "price": "0.44",
            "size": "2",
            "slug": "market",
            "outcome": "Yes",
        }
        duplicate = {**first, "transactionHash": "tx2", "proxyWallet": WALLET_2, "timestamp": 101}

        accepted = copy_trade_preview_from_activity(cfg, FakeRegistry(FakePolymarketAdapter()), first, conflict_state)
        skipped = copy_trade_preview_from_activity(cfg, FakeRegistry(FakePolymarketAdapter()), duplicate, conflict_state)

        self.assertEqual(accepted["status"], "simulation")
        self.assertEqual(skipped["status"], "skipped")
        self.assertIn("duplicate", skipped["reason"])

    def test_polymarket_leaderboard_payload_computes_roi_and_scans_pages(self) -> None:
        first_page = [
            {"rank": index, "proxyWallet": f"0x{index:040x}", "pseudonym": f"user-{index}", "pnl": "1", "volume": "100"}
            for index in range(1, 51)
        ]
        first_page[0] = {"rank": 1, "proxyWallet": "0xaaa", "pseudonym": "alpha", "pnl": "10", "volume": "100"}
        pages = [
            first_page,
            [
                {"rank": 51, "proxyWallet": "0xccc", "pseudonym": "gamma", "pnl": "4", "volume": "20"},
            ],
        ]

        def fake_leaderboard(*_args, **kwargs):
            return pages[0] if kwargs["offset"] == 0 else pages[1]

        with patch("web_api.data_api.get_leaderboard", side_effect=fake_leaderboard) as mock_get:
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["roi_pct"],
                    "limit": ["2"],
                    "scan_limit": ["51"],
                    "min_volume_usd": ["20"],
                }
            )

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(payload["counts"]["scanned"], 51)
        self.assertEqual(payload["rows"][0]["display_name"], "gamma")
        self.assertAlmostEqual(payload["rows"][0]["roi_pct"], 20.0)
        self.assertEqual(payload["rows"][1]["display_name"], "alpha")
        self.assertFalse(payload["mdd_available"])
        self.assertEqual(payload["source_sort"], "PNL")

    def test_polymarket_leaderboard_payload_uses_full_wallet_display_fallback(self) -> None:
        leaderboard = [{"rank": 1, "proxyWallet": WALLET, "pnl": "10", "volume": "100"}]

        with patch("web_api.data_api.get_leaderboard", return_value=leaderboard):
            payload = polymarket_leaderboard_payload({"sort": ["roi_pct"], "limit": ["1"], "scan_limit": ["1"]})

        self.assertEqual(payload["rows"][0]["wallet"], WALLET)
        self.assertEqual(payload["rows"][0]["display_name"], WALLET)

    def test_polymarket_leaderboard_payload_can_cancel_after_current_page(self) -> None:
        page_calls = 0
        first_page = [
            {"rank": index, "proxyWallet": f"0x{index:040x}", "pseudonym": f"user-{index}", "pnl": "1", "volume": "100"}
            for index in range(1, 51)
        ]

        def fake_leaderboard(*_args, **_kwargs):
            nonlocal page_calls
            page_calls += 1
            return first_page

        def cancel_after_first_page() -> bool:
            return page_calls >= 1

        with patch("web_api.data_api.get_leaderboard", side_effect=fake_leaderboard) as mock_get:
            payload = polymarket_leaderboard_payload(
                {"sort": ["roi_pct"], "limit": ["10"], "scan_limit": ["100"]},
                cancel_check=cancel_after_first_page,
            )

        mock_get.assert_called_once()
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["counts"]["scanned"], 50)
        self.assertEqual(payload["counts"]["returned"], 10)
        self.assertIn("cancelled", payload["warnings"][0])

    def test_polymarket_leaderboard_payload_retries_transient_page_error(self) -> None:
        calls = 0
        progress: list[dict] = []

        def fake_leaderboard(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("ssl eof")
            return [{"rank": 1, "proxyWallet": WALLET, "pnl": "10", "volume": "100"}]

        with patch("web_api.data_api.get_leaderboard", side_effect=fake_leaderboard):
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["roi_pct"],
                    "limit": ["1"],
                    "scan_limit": ["1"],
                    "scan_retry_attempts": ["2"],
                    "scan_retry_delay_seconds": ["0"],
                },
                progress_callback=progress.append,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(payload["counts"]["scanned"], 1)
        self.assertEqual(payload["scan_retry_attempts"], 2)
        self.assertTrue(any("retrying" in warning for warning in payload["warnings"]))
        self.assertTrue(any("retrying" in item.get("message", "") for item in progress))

    def test_leaderboard_scanner_can_checkpoint_without_retaining_pages(self) -> None:
        captured_pages = []
        progress = []
        page = [{"rank": 1, "proxyWallet": WALLET, "pnl": "10", "volume": "100"}]

        with patch("web_api.data_api.get_leaderboard", return_value=page):
            rows, cancelled = _fetch_polymarket_leaderboard_scan_rows(
                scan_limit=1,
                retain_rows=False,
                remote_sort="PNL",
                direction="DESC",
                period="all",
                category="OVERALL",
                scan_concurrency=1,
                is_cancelled=lambda: False,
                emit_progress=lambda _phase, **values: progress.append(values),
                warnings=[],
                page_callback=lambda offset, limit, rows: captured_pages.append((offset, limit, rows)),
            )

        self.assertEqual(rows, [])
        self.assertFalse(cancelled)
        self.assertEqual(captured_pages, [(0, 1, page)])
        self.assertEqual(progress[-1]["scanned"], 1)

    def test_leaderboard_scanner_stops_on_a_repeated_full_page(self) -> None:
        page = [
            {"rank": index + 1, "proxyWallet": f"0x{index:040x}", "pnl": str(index), "volume": "100"}
            for index in range(50)
        ]
        progress = []
        warnings = []
        summary = {}

        with patch("web_api.data_api.get_leaderboard", return_value=page) as mock_get:
            rows, cancelled = _fetch_polymarket_leaderboard_scan_rows(
                scan_limit=None,
                retain_rows=True,
                remote_sort="PNL",
                direction="DESC",
                period="all",
                category="OVERALL",
                scan_concurrency=1,
                is_cancelled=lambda: False,
                emit_progress=lambda _phase, **values: progress.append(values),
                warnings=warnings,
                scan_summary=summary,
            )

        self.assertFalse(cancelled)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(rows, page)
        self.assertEqual(summary["completion_reason"], "repeated_page")
        self.assertFalse(summary["source_enumeration_complete"])
        self.assertTrue(any("repeated" in warning for warning in warnings))
        self.assertTrue(any("repeated" in item.get("message", "") for item in progress))

    def test_leaderboard_payload_marks_a_repeated_page_as_incomplete_source_enumeration(self) -> None:
        page = [
            {"rank": index + 1, "proxyWallet": f"0x{index:040x}", "pnl": str(index), "volume": "100"}
            for index in range(50)
        ]

        with patch("web_api.data_api.get_leaderboard", return_value=page) as mock_get:
            payload = polymarket_leaderboard_payload({"limit": ["all"], "scan_limit": ["unlimited"]})

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(payload["counts"]["scanned"], 50)
        self.assertEqual(payload["completion_reason"], "repeated_page")
        self.assertFalse(payload["source_enumeration_complete"])
        self.assertIn("public Polymarket leaderboard", payload["source_scope_note"])

    def test_polymarket_leaderboard_payload_resumes_from_checkpoint_rows(self) -> None:
        checkpoint_rows = [{"rank": 1, "proxyWallet": WALLET, "pnl": "10", "volume": "100"}]
        page_callbacks = []

        def fake_leaderboard(*_args, **kwargs):
            self.assertEqual(kwargs["offset"], 1)
            return []

        with patch("web_api.data_api.get_leaderboard", side_effect=fake_leaderboard) as mock_get:
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["roi_pct"],
                    "limit": ["all"],
                    "scan_limit": ["all"],
                    "scan_start_offset": ["1"],
                },
                initial_raw_rows=checkpoint_rows,
                leaderboard_page_callback=lambda offset, limit, rows: page_callbacks.append((offset, limit, rows)),
            )

        mock_get.assert_called_once()
        self.assertEqual(payload["counts"]["scanned"], 1)
        self.assertEqual(payload["counts"]["returned"], 1)
        self.assertEqual(payload["scan_start_offset"], 1)
        self.assertEqual(payload["initial_checkpoint_rows"], 1)
        self.assertEqual(payload["rows"][0]["wallet"], WALLET)
        self.assertEqual(page_callbacks[0][0], 1)

    def test_polymarket_leaderboard_payload_does_not_cap_deep_scan_values(self) -> None:
        with patch("web_api.data_api.get_leaderboard", return_value=[]) as mock_get:
            payload = polymarket_leaderboard_payload(
                {
                    "limit": ["2000000"],
                    "scan_limit": ["2000000"],
                    "mdd_scan_limit": ["2000000"],
                    "compute_mdd": ["true"],
                }
            )

        self.assertEqual(payload["limit"], 2_000_000)
        self.assertEqual(payload["scan_limit"], 2_000_000)
        self.assertEqual(payload["mdd_scan_limit"], 2_000_000)
        self.assertFalse(payload["limit_unlimited"])
        self.assertFalse(payload["scan_limit_unlimited"])
        self.assertFalse(payload["mdd_scan_limit_unlimited"])
        self.assertEqual(payload["completion_reason"], "end_of_results")
        self.assertTrue(payload["source_enumeration_complete"])
        self.assertIn("do not establish coverage", payload["source_scope_note"])
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs["limit"], 50)

    def test_polymarket_leaderboard_payload_accepts_unlimited_limits(self) -> None:
        full_page = [
            {"rank": index, "proxyWallet": f"0x{index:040x}", "pseudonym": f"user-{index}", "pnl": "1", "volume": "100"}
            for index in range(1, 51)
        ]
        tail_page = [
            {"rank": 51, "proxyWallet": WALLET, "pseudonym": "alpha", "pnl": "10", "volume": "100"},
            {"rank": 52, "proxyWallet": WALLET_2, "pseudonym": "beta", "pnl": "20", "volume": "500"},
        ]

        def fake_leaderboard(*_args, **kwargs):
            return full_page if kwargs["offset"] == 0 else tail_page

        def fake_mdd(wallet, **_kwargs):
            return {
                "mdd_usd": 10.0,
                "mdd_pct": 5.0,
                "mdd_available": True,
                "mdd_method": "test",
                "mdd_pct_basis": "test",
                "points": [{"value": 0}],
                "closed_positions": 2,
                "open_positions": 1,
                "equity_base_usd": 1000,
                "peak_value": 100,
                "trough_value": 95,
                "peak_timestamp": 10,
                "trough_timestamp": 20,
            }

        with patch("web_api.data_api.get_leaderboard", side_effect=fake_leaderboard) as mock_get, patch(
            "web_api.polymarket_user_mdd_payload",
            side_effect=fake_mdd,
        ) as mock_mdd:
            payload = polymarket_leaderboard_payload(
                {
                    "limit": ["all"],
                    "scan_limit": ["unlimited"],
                    "mdd_scan_limit": ["0"],
                    "compute_mdd": ["true"],
                }
            )

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_mdd.call_count, 52)
        self.assertIsNone(payload["limit"])
        self.assertIsNone(payload["scan_limit"])
        self.assertIsNone(payload["mdd_scan_limit"])
        self.assertTrue(payload["limit_unlimited"])
        self.assertTrue(payload["scan_limit_unlimited"])
        self.assertTrue(payload["mdd_scan_limit_unlimited"])
        self.assertEqual(payload["counts"]["returned"], 52)
        self.assertEqual(payload["counts"]["scanned"], 52)
        self.assertEqual(payload["counts"]["mdd_computed"], 52)
        self.assertEqual(payload["completion_reason"], "end_of_results")
        self.assertTrue(payload["source_enumeration_complete"])
        self.assertIn("every Polymarket account", payload["source_scope_note"])

    def test_polymarket_user_mdd_payload_computes_usd_and_percentage_drawdown(self) -> None:
        with patch(
            "web_api.data_api.get_closed_positions",
            return_value=[
                {"timestamp": 10, "realizedPnl": "100", "totalBought": "1000"},
                {"timestamp": 20, "realizedPnl": "-40", "totalBought": "500"},
            ],
        ), patch(
            "web_api.data_api.get_positions",
            return_value=[{"totalPnl": "-10", "currentValue": "20", "initialValue": "100"}],
        ), patch(
            "web_api.data_api.get_activity",
            return_value=[],
        ), patch(
            "web_api.data_api.get_trades",
            return_value=[],
        ):
            payload = polymarket_user_mdd_payload(WALLET, closed_limit=10)

        self.assertTrue(payload["mdd_available"])
        self.assertEqual(payload["mdd_method"], MDD_METHOD_V2)
        self.assertEqual(payload["closed_positions"], 2)
        self.assertEqual(payload["open_positions"], 1)
        self.assertAlmostEqual(payload["mdd_usd"], 50.0)
        self.assertAlmostEqual(payload["mdd_pct"], 50.0 / 1700.0 * 100.0)
        self.assertEqual(payload["peak_value"], 100.0)
        self.assertEqual(payload["trough_value"], 50.0)
        self.assertIn("assumptions", payload)
        self.assertEqual(payload["trade_capital"]["events"], 0)

    def test_polymarket_user_mdd_payload_uses_accounting_snapshot_equity_base_when_requested(self) -> None:
        snapshot = self._accounting_zip(
            "timestamp,equity,deposits,withdrawals\n10,1000,1000,0\n20,1200,0,0\n",
            "asset,currentValue,realizedPnl\nasset-1,20,60\n",
        )
        with patch(
            "web_api.data_api.get_closed_positions",
            return_value=[
                {"timestamp": 10, "realizedPnl": "100", "totalBought": "100"},
                {"timestamp": 20, "realizedPnl": "-40", "totalBought": "100"},
            ],
        ), patch(
            "web_api.data_api.get_positions",
            return_value=[{"totalPnl": "0", "currentValue": "20", "initialValue": "50"}],
        ), patch(
            "web_api.data_api.get_activity",
            return_value=[],
        ), patch(
            "web_api.data_api.get_trades",
            return_value=[],
        ), patch(
            "polymarket.accounting.data_api.download_accounting_snapshot",
            return_value=snapshot,
        ) as mock_snapshot:
            payload = polymarket_user_mdd_payload(WALLET, closed_limit=10, include_accounting_snapshot=True)

        mock_snapshot.assert_called_once()
        self.assertEqual(payload["equity_base_source"], "accounting_snapshot_max_equity")
        self.assertEqual(payload["equity_base_usd"], 1200.0)
        self.assertAlmostEqual(payload["mdd_usd"], 40.0)
        self.assertAlmostEqual(payload["mdd_pct"], 40.0 / 1300.0 * 100.0)
        self.assertEqual(payload["accounting_snapshot"]["status"], "ok")
        self.assertTrue(payload["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_polymarket_user_mdd_payload_uses_trade_activity_for_public_capital_basis(self) -> None:
        activity_pages = [
            [
                {
                    "type": "TRADE",
                    "side": "BUY",
                    "timestamp": 5,
                    "size": "200",
                    "price": "0.50",
                    "transactionHash": "0xbuy",
                    "asset": "token-1",
                },
                {
                    "type": "TRADE",
                    "side": "SELL",
                    "timestamp": 15,
                    "usdcSize": "40",
                    "transactionHash": "0xsell",
                    "asset": "token-1",
                },
                {"type": "REWARD", "timestamp": 16, "usdcSize": "3"},
            ],
            [],
        ]

        def fake_activity(*_args, **kwargs):
            return activity_pages[0] if kwargs["offset"] == 0 else activity_pages[1]

        with patch(
            "web_api.data_api.get_closed_positions",
            return_value=[
                {"timestamp": 10, "realizedPnl": "50", "totalBought": "25"},
                {"timestamp": 20, "realizedPnl": "-20", "totalBought": "25"},
            ],
        ), patch(
            "web_api.data_api.get_positions",
            return_value=[],
        ), patch(
            "web_api.data_api.get_activity",
            side_effect=fake_activity,
        ), patch(
            "web_api.data_api.get_trades",
            return_value=[],
        ):
            payload = polymarket_user_mdd_payload(WALLET, closed_limit=10, activity_limit=1000, trade_limit=0)

        self.assertEqual(payload["activity_events"], 3)
        self.assertEqual(payload["trade_events"], 0)
        self.assertEqual(payload["trade_capital"]["events"], 2)
        self.assertAlmostEqual(payload["trade_capital"]["buy_notional_usd"], 100.0)
        self.assertAlmostEqual(payload["trade_capital"]["sell_notional_usd"], 40.0)
        self.assertAlmostEqual(payload["trade_capital"]["max_deployed_notional_usd"], 100.0)
        self.assertAlmostEqual(payload["public_capital_basis_usd"], 100.0)
        self.assertAlmostEqual(payload["mdd_usd"], 20.0)
        self.assertAlmostEqual(payload["mdd_pct"], 20.0 / 150.0 * 100.0)

    def test_polymarket_user_mdd_payload_mark_replay_uses_clob_price_history(self) -> None:
        trade = {
            "type": "TRADE",
            "side": "BUY",
            "timestamp": 10,
            "size": "100",
            "price": "0.50",
            "transactionHash": "0xbuy",
            "asset": "token-1",
        }
        history = {
            "history": {
                "token-1": [
                    {"t": 10, "p": 0.50},
                    {"t": 20, "p": 0.20},
                    {"t": 30, "p": 0.80},
                ]
            }
        }
        with patch("web_api.data_api.get_closed_positions", return_value=[]), patch(
            "web_api.data_api.get_positions",
            return_value=[],
        ), patch(
            "web_api.data_api.get_activity",
            return_value=[trade],
        ), patch(
            "web_api.data_api.get_trades",
            return_value=[],
        ), patch(
            "polymarket.mdd.clob_rest.get_batch_price_history",
            return_value=history,
        ) as mock_history:
            payload = polymarket_user_mdd_payload(
                WALLET,
                mode="mark_replay",
                activity_limit=10,
                trade_limit=0,
                mark_replay_token_limit=20,
                mark_replay_interval="1h",
                mark_replay_fidelity=60,
            )

        mock_history.assert_called_once()
        self.assertEqual(payload["mdd_method"], MDD_METHOD_MARK_REPLAY)
        self.assertEqual(payload["mark_replay"]["status"], "ok")
        self.assertEqual(payload["mark_replay"]["token_count"], 1)
        self.assertEqual(payload["mark_replay"]["batch_cap"], 20)
        self.assertAlmostEqual(payload["mdd_usd"], 30.0)
        self.assertAlmostEqual(payload["mdd_pct"], 30.0 / 50.0 * 100.0)
        self.assertEqual(payload["peak_value"], 0.0)
        self.assertEqual(payload["trough_value"], -30.0)
        self.assertEqual(payload["fallback_v2"]["mdd_method"], MDD_METHOD_V2)
        self.assertGreaterEqual(payload["points_total"], 3)

    def test_polymarket_user_mdd_payload_mark_replay_reports_unreconstructable_tokens(self) -> None:
        trade_rows = [
            {"side": "BUY", "timestamp": 10, "size": "10", "price": "0.40", "asset": f"token-{index}"}
            for index in range(22)
        ]
        history = {"history": {"token-0": [{"t": 10, "p": 0.40}]}}
        with patch("web_api.data_api.get_closed_positions", return_value=[]), patch(
            "web_api.data_api.get_positions",
            return_value=[],
        ), patch(
            "web_api.data_api.get_activity",
            return_value=trade_rows,
        ), patch(
            "web_api.data_api.get_trades",
            return_value=[],
        ), patch(
            "polymarket.mdd.clob_rest.get_batch_price_history",
            return_value=history,
        ):
            payload = polymarket_user_mdd_payload(WALLET, mode="mark_replay", activity_limit=100, mark_replay_token_limit=20)

        self.assertEqual(payload["mdd_method"], MDD_METHOD_MARK_REPLAY)
        self.assertEqual(payload["mark_replay"]["status"], "partial")
        self.assertEqual(payload["mark_replay"]["token_count"], 20)
        self.assertIn("token-20", payload["mark_replay"]["clipped_token_ids"])
        self.assertIn("token-1", payload["mark_replay"]["missing_history_tokens"])

    def test_polymarket_leaderboard_payload_computes_and_sorts_mdd_filter(self) -> None:
        leaderboard = [
            {"rank": 1, "proxyWallet": WALLET, "pseudonym": "alpha", "pnl": "10", "volume": "100"},
            {"rank": 2, "proxyWallet": WALLET_2, "pseudonym": "beta", "pnl": "20", "volume": "500"},
        ]

        def fake_mdd(wallet, **_kwargs):
            value = 6.0 if wallet == WALLET else 12.0
            return {
                "mdd_usd": value * 10,
                "mdd_pct": value,
                "mdd_available": True,
                "mdd_method": "test",
                "mdd_pct_basis": "test",
                "points": [{"value": 0}],
                "closed_positions": 2,
                "open_positions": 1,
                "equity_base_usd": 1000,
                "peak_value": 100,
                "trough_value": 100 - value,
                "peak_timestamp": 10,
                "trough_timestamp": 20,
            }

        with patch(
            "web_api.data_api.get_leaderboard",
            return_value=leaderboard,
        ), patch("web_api.polymarket_user_mdd_payload", side_effect=fake_mdd):
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["mdd_pct"],
                    "direction": ["DESC"],
                    "limit": ["2"],
                    "scan_limit": ["2"],
                    "mdd_scan_limit": ["2"],
                    "min_mdd_pct": ["5"],
                }
            )

        self.assertEqual(payload["counts"]["mdd_computed"], 2)
        self.assertEqual(payload["counts"]["returned"], 2)
        self.assertEqual(payload["rows"][0]["wallet"], WALLET_2)
        self.assertAlmostEqual(payload["rows"][0]["mdd_pct"], 12.0)
        self.assertTrue(payload["mdd_available"])

    def test_polymarket_leaderboard_payload_applies_mdd_budget_after_local_roi_sort(self) -> None:
        leaderboard = [
            {"rank": 1, "proxyWallet": WALLET, "pseudonym": "high-pnl-low-roi", "pnl": "1000", "volume": "100000"},
            {"rank": 2, "proxyWallet": WALLET_2, "pseudonym": "lower-pnl-high-roi", "pnl": "100", "volume": "200"},
        ]

        def fake_mdd(wallet, **_kwargs):
            return {
                "mdd_usd": 10.0,
                "mdd_pct": 5.0,
                "mdd_available": True,
                "mdd_method": "test",
                "mdd_pct_basis": "test",
                "points": [{"value": 0}],
                "closed_positions": 2,
                "open_positions": 1,
                "equity_base_usd": 1000,
                "peak_value": 100,
                "trough_value": 95,
                "peak_timestamp": 10,
                "trough_timestamp": 20,
            }

        with patch("web_api.data_api.get_leaderboard", return_value=leaderboard), patch(
            "web_api.polymarket_user_mdd_payload",
            side_effect=fake_mdd,
        ) as mock_mdd:
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["roi_pct"],
                    "direction": ["DESC"],
                    "limit": ["1"],
                    "scan_limit": ["2"],
                    "mdd_scan_limit": ["1"],
                    "compute_mdd": ["true"],
                }
            )

        mock_mdd.assert_called_once()
        self.assertEqual(mock_mdd.call_args.args[0], WALLET_2)
        self.assertEqual(payload["counts"]["mdd_computed"], 1)
        self.assertEqual(payload["rows"][0]["wallet"], WALLET_2)

    def test_polymarket_leaderboard_payload_fast_mdd_stops_after_enough_qualified_rows(self) -> None:
        leaderboard = [
            {"rank": 1, "proxyWallet": WALLET, "pseudonym": "top-roi", "pnl": "200", "volume": "400"},
            {"rank": 2, "proxyWallet": WALLET_2, "pseudonym": "next-roi", "pnl": "100", "volume": "400"},
        ]

        def fake_mdd(wallet, **_kwargs):
            return {
                "mdd_usd": 10.0,
                "mdd_pct": 10.0,
                "mdd_available": True,
                "mdd_method": "test",
                "mdd_pct_basis": "test",
                "points": [{"value": 0}],
                "closed_positions": 2,
                "open_positions": 1,
                "equity_base_usd": 1000,
                "peak_value": 100,
                "trough_value": 90,
                "peak_timestamp": 10,
                "trough_timestamp": 20,
            }

        with patch("web_api.data_api.get_leaderboard", return_value=leaderboard), patch(
            "web_api.polymarket_user_mdd_payload",
            side_effect=fake_mdd,
        ) as mock_mdd:
            payload = polymarket_leaderboard_payload(
                {
                    "sort": ["roi_pct"],
                    "direction": ["DESC"],
                    "limit": ["1"],
                    "scan_limit": ["2"],
                    "mdd_scan_limit": ["2"],
                    "compute_mdd": ["true"],
                    "max_mdd_pct": ["20"],
                    "fast_scan": ["true"],
                    "mdd_concurrency": ["1"],
                }
            )

        mock_mdd.assert_called_once()
        self.assertEqual(mock_mdd.call_args.args[0], WALLET)
        self.assertEqual(payload["counts"]["returned"], 1)
        self.assertEqual(payload["counts"]["mdd_attempted"], 1)
        self.assertEqual(payload["counts"]["mdd_qualified"], 1)
        self.assertTrue(payload["mdd_stop_on_limit"])

    def test_polymarket_leaderboard_payload_persists_mdd_audit_cache_when_requested(self) -> None:
        leaderboard = [{"rank": 1, "proxyWallet": WALLET, "pseudonym": "alpha", "pnl": "10", "volume": "100"}]

        def fake_mdd(wallet, **_kwargs):
            return {
                "wallet": wallet,
                "mdd_usd": 50.0,
                "mdd_pct": 5.0,
                "mdd_available": True,
                "mdd_method": "test",
                "mdd_pct_basis": "test",
                "points": [{"timestamp": 10, "value": 100.0}],
                "closed_positions": 2,
                "open_positions": 1,
                "equity_base_usd": 1000.0,
                "peak_value": 100.0,
                "trough_value": 50.0,
                "peak_timestamp": 10,
                "trough_timestamp": 20,
            }

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"POLYMARKET_ANALYTICS_CACHE_PATH": str(Path(tmp) / "analytics-cache.json")},
        ), patch("web_api.data_api.get_leaderboard", return_value=leaderboard), patch(
            "web_api.polymarket_user_mdd_payload", side_effect=fake_mdd
        ):
            payload = polymarket_leaderboard_payload(
                {
                    "compute_mdd": ["true"],
                    "limit": ["1"],
                    "scan_limit": ["1"],
                    "mdd_scan_limit": ["1"],
                    "mdd_persist_cache": ["true"],
                }
            )
            cache_key = payload["rows"][0]["mdd_audit_cache_key"]
            export = polymarket_mdd_export_payload(cache_key)

        self.assertTrue(payload["analytics_cache"]["enabled"])
        self.assertTrue(payload["rows"][0]["mdd_audit_cache_stored"])
        self.assertEqual(export["payload"]["wallet"], WALLET)
        self.assertEqual(export["cache"]["key"], cache_key)

    def test_polymarket_leaderboard_payload_reports_rate_limit_without_more_mdd_calls(self) -> None:
        leaderboard = [
            {"rank": 1, "proxyWallet": WALLET, "pseudonym": "alpha", "pnl": "10", "volume": "100"},
            {"rank": 2, "proxyWallet": WALLET_2, "pseudonym": "beta", "pnl": "20", "volume": "500"},
        ]
        exc = PolymarketRateLimitError(
            "limited",
            service="data",
            method="GET",
            url="https://data-api.polymarket.com/test",
            status_code=429,
        )
        with patch("web_api.data_api.get_leaderboard", return_value=leaderboard), patch(
            "web_api.polymarket_user_mdd_payload",
            side_effect=exc,
        ) as mock_mdd:
            payload = polymarket_leaderboard_payload(
                {"compute_mdd": ["true"], "limit": ["2"], "scan_limit": ["2"], "mdd_scan_limit": ["2"]}
            )

        mock_mdd.assert_called_once()
        self.assertTrue(payload["rate_limit"]["limited"])
        self.assertEqual(payload["rate_limit"]["events"][0]["status_code"], 429)
        self.assertIn("rate-limited", payload["warnings"][0])

    def test_polymarket_mdd_csv_export_route_serves_cached_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            frontend_dir = root / "dist"
            frontend_dir.mkdir()
            save_config(AppConfig(), config_path)
            with patch.dict(os.environ, {"POLYMARKET_ANALYTICS_CACHE_PATH": str(root / "analytics-cache.json")}):
                metadata = store_analytics_artifact(
                    POLYMARKET_MDD_AUDIT_KIND,
                    {"wallet": WALLET, "mode": "fast"},
                    {
                        "wallet": WALLET,
                        "mdd_method": "test",
                        "mdd_available": True,
                        "mdd_usd": 10.0,
                        "mdd_pct": 1.0,
                        "equity_base_usd": 1000.0,
                        "peak_value": 10.0,
                        "trough_value": 0.0,
                        "mdd_pct_basis": "test",
                        "points": [{"timestamp": 1, "value": 10.0}],
                    },
                )
                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/users/mdd/export.csv?key={metadata['key']}",
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertIn(b"summary,", body)
        self.assertIn(WALLET.encode("utf-8"), body)

    def test_polymarket_direct_mdd_route_persists_cache_and_json_export(self) -> None:
        def fake_mdd(wallet, **_kwargs):
            return {
                "wallet": wallet,
                "mdd_method": "test",
                "mdd_available": True,
                "mdd_usd": 25.0,
                "mdd_pct": 2.5,
                "mdd_pct_basis": "test",
                "points": [{"timestamp": 1, "value": 25.0}],
                "closed_positions": 1,
                "open_positions": 0,
                "equity_base_usd": 1000.0,
                "peak_value": 25.0,
                "trough_value": 0.0,
                "peak_timestamp": 1,
                "trough_timestamp": 2,
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            frontend_dir = root / "dist"
            frontend_dir.mkdir()
            save_config(AppConfig(), config_path)
            with patch.dict(os.environ, {"POLYMARKET_ANALYTICS_CACHE_PATH": str(root / "analytics-cache.json")}), patch(
                "web_api.polymarket_user_mdd_payload",
                side_effect=fake_mdd,
            ):
                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    status, mdd = self._request_json(base_url, f"/api/polymarket/users/mdd?wallet={WALLET}&persist_cache=true")
                    cache_key = mdd["audit_cache"]["key"]
                    export_status, export = self._request_json(base_url, f"/api/polymarket/users/mdd/export.json?key={cache_key}")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        self.assertEqual(status, 200)
        self.assertTrue(mdd["audit_cache"]["stored"])
        self.assertEqual(export_status, 200)
        self.assertEqual(export["payload"]["wallet"], WALLET)
        self.assertEqual(export["cache"]["key"], cache_key)

    def test_polymarket_mdd_cache_routes_list_health_and_purge_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            frontend_dir = root / "dist"
            cache_path = root / "analytics-cache.json"
            frontend_dir.mkdir()
            save_config(AppConfig(), config_path)
            with patch.dict(os.environ, {"POLYMARKET_ANALYTICS_CACHE_PATH": str(cache_path)}):
                fresh = store_analytics_artifact(
                    POLYMARKET_MDD_AUDIT_KIND,
                    {"wallet": WALLET, "mode": "fast"},
                    {
                        "wallet": WALLET,
                        "mdd_method": "test",
                        "mdd_available": True,
                        "mdd_usd": 10.0,
                        "mdd_pct": 1.0,
                        "equity_base_usd": 1000.0,
                        "points": [{"timestamp": 1, "value": 10.0}],
                    },
                )
                expired = store_analytics_artifact(
                    POLYMARKET_MDD_AUDIT_KIND,
                    {"wallet": WALLET_2, "mode": "fast"},
                    {
                        "wallet": WALLET_2,
                        "mdd_method": "test",
                        "mdd_available": True,
                        "mdd_usd": 20.0,
                        "mdd_pct": 2.0,
                        "equity_base_usd": 1000.0,
                        "points": [{"timestamp": 1, "value": 20.0}],
                    },
                )
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                cache["entries"][expired["key"]]["expires_at"] = 1
                cache_path.write_text(json.dumps(cache), encoding="utf-8")

                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    list_status, listing = self._request_json(base_url, "/api/polymarket/users/mdd/cache")
                    health_status, health = self._request_json(base_url, "/api/polymarket/users/mdd/cache/health")
                    purge_status, purge = self._request_json(
                        base_url,
                        "/api/polymarket/users/mdd/cache/purge",
                        method="POST",
                        payload={"expired_only": True},
                    )
                    delete_status, deleted = self._request_json(
                        base_url,
                        f"/api/polymarket/users/mdd/cache/{fresh['key']}",
                        method="DELETE",
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

        self.assertEqual(list_status, 200)
        self.assertEqual(listing["counts"]["entries"], 2)
        self.assertEqual(listing["counts"]["expired_entries"], 1)
        self.assertEqual(health_status, 200)
        self.assertEqual(health["cache"]["entries"], 2)
        self.assertEqual(health["cache"]["expired_entries"], 1)
        self.assertEqual(purge_status, 200)
        self.assertIn(expired["key"], purge["deleted_keys"])
        self.assertEqual(purge["counts"]["entries"], 1)
        self.assertEqual(delete_status, 200)
        self.assertIn(fresh["key"], deleted["deleted_keys"])
        self.assertEqual(deleted["counts"]["entries"], 0)

    def test_polymarket_user_search_payload_returns_profile_rows(self) -> None:
        with patch(
            "web_api.gamma.search_profiles",
            return_value=[
                ProfileResult(
                    pseudonym="Trader",
                    proxy_wallet=WALLET,
                    profile_image="https://example.test/avatar.png",
                    display_username_public=True,
                )
            ],
        ) as mock_search:
            payload = polymarket_user_search_payload("trade", limit=3)

        mock_search.assert_called_once_with("trade", limit=3)
        self.assertEqual(payload["counts"]["profiles"], 1)
        self.assertEqual(payload["profiles"][0]["proxy_wallet"], WALLET)

    def test_apply_config_patch_validates_selected_market_theme_and_ui_design(self) -> None:
        cfg = AppConfig()

        apply_config_patch(cfg, {"selected_market_id": "kalshi", "theme": "dark", "ui_design": "sentinel_2027"})

        self.assertEqual(cfg.selected_market_id, "kalshi")
        self.assertEqual(cfg.theme, "dark")
        self.assertEqual(cfg.ui_design, "sentinel_2027")
        with self.assertRaises(ValueError):
            apply_config_patch(cfg, {"selected_market_id": "missing"})
        with self.assertRaises(ValueError):
            apply_config_patch(cfg, {"theme": "blue"})
        with self.assertRaises(ValueError):
            apply_config_patch(cfg, {"ui_design": "missing"})

    def test_apply_market_patch_updates_enabled_and_settings(self) -> None:
        cfg = AppConfig()

        apply_market_patch(cfg, "kalshi", {"enabled": True, "settings": {"max_size": 3}})

        self.assertTrue(cfg.markets["kalshi"].enabled)
        self.assertEqual(cfg.markets["kalshi"].settings["max_size"], 3)
        with self.assertRaises(ValueError):
            apply_market_patch(cfg, "missing", {"enabled": True})
        with self.assertRaises(ValueError):
            apply_market_patch(cfg, "kalshi", {"settings": "bad"})

    def test_apply_market_patch_persists_validated_live_safety_fields(self) -> None:
        cfg = AppConfig()

        apply_market_patch(
            cfg,
            "kalshi",
            {
                "enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_kill_switch": False,
                "live_trading_max_size": "9",
                "live_trading_max_notional": "25.5",
            },
        )

        settings = cfg.markets["kalshi"].settings
        self.assertTrue(cfg.markets["kalshi"].enabled)
        self.assertTrue(settings["live_trading_enabled"])
        self.assertTrue(settings["live_trading_confirmed"])
        self.assertFalse(settings["live_trading_kill_switch"])
        self.assertEqual(settings["live_trading_max_size"], 9.0)
        self.assertEqual(settings["live_trading_max_notional"], 25.5)

        apply_market_patch(cfg, "kalshi", {"live_trading_max_size": "", "live_trading_max_notional": None})

        self.assertNotIn("live_trading_max_size", cfg.markets["kalshi"].settings)
        self.assertNotIn("live_trading_max_notional", cfg.markets["kalshi"].settings)
        with self.assertRaises(ValueError):
            apply_market_patch(cfg, "kalshi", {"live_trading_max_size": "-1"})

    def test_live_safety_payload_reports_selected_gate_state(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        apply_market_patch(
            cfg,
            "kalshi",
            {
                "enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_kill_switch": False,
                "live_trading_max_size": "5",
                "live_trading_max_notional": "10",
            },
        )

        payload = live_safety_payload(cfg, FakeRegistry(FakePaperAdapter()))

        self.assertEqual(payload["selected_market_id"], "kalshi")
        self.assertEqual(payload["status"], "armed")
        self.assertEqual(payload["tone"], "good")
        self.assertTrue(payload["can_preflight"])
        self.assertEqual(payload["controls"]["live_trading_max_size"], 5.0)
        self.assertEqual(payload["controls"]["live_trading_max_notional"], 10.0)
        self.assertEqual(payload["blockers"], [])

    def test_live_preflight_payload_returns_redacted_audit_without_ordering(self) -> None:
        adapter = FakePaperAdapter()
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        apply_market_patch(
            cfg,
            "kalshi",
            {
                "enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": "5",
                "live_trading_max_notional": "10",
            },
        )

        payload = live_preflight_payload(
            cfg,
            FakeRegistry(adapter),
            {
                "market_id": "kalshi",
                "contract_id": "KALSHI-CONTRACT",
                "side": "BUY",
                "size": "2",
                "limit_price": "0.5",
                "metadata": {"client_order_id": "order-1", "private_key": "super-secret"},
            },
        )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["blocked"])
        self.assertEqual(adapter.orders, [])
        self.assertEqual(payload["preflight"]["feature"], "live preflight preview")
        self.assertEqual(payload["preflight"]["metadata_keys"], ["client_order_id", "private_key"])
        self.assertIn("Preflight OK", payload["message"])
        self.assertNotIn("super-secret", json.dumps(payload))

    def test_live_preflight_payload_returns_blocked_gate_audit(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        apply_market_patch(
            cfg,
            "kalshi",
            {
                "enabled": True,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "live_trading_max_size": "1",
            },
        )

        payload = live_preflight_payload(
            cfg,
            FakeRegistry(FakePaperAdapter()),
            {
                "market_id": "kalshi",
                "contract_id": "KALSHI-CONTRACT",
                "side": "BUY",
                "size": "2",
                "limit_price": "0.5",
            },
        )

        self.assertFalse(payload["ok"])
        self.assertTrue(payload["blocked"])
        self.assertIn("exceeds configured max", payload["message"])
        self.assertEqual(payload["live_safety"]["status"], "armed")

    def test_api_payloads_roundtrip_with_file_storage(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            save_config(cfg, path)
            loaded = load_config(path)

        self.assertEqual(markets_payload(loaded)["selected_market_id"], "kalshi")
        self.assertEqual(len(paper_position_rows(loaded.paper_trades)), 1)

    def test_health_payload_documents_parallel_gui_contract(self) -> None:
        with patch("web_api.project_version", return_value="9.8.7"):
            payload = health_payload(Path("local-config.json"), Path("frontend-dist"))

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["api_version"], "9.8.7")
        self.assertEqual(payload["mode"], "parallel")
        self.assertTrue(payload["python_gui_available"])
        self.assertEqual(payload["python_gui_command"], "python app.py")
        self.assertEqual(payload["python_gui_script"], "run_gui.bat")
        self.assertEqual(payload["tkinter_fallback"], "run_gui.bat or python app.py")
        self.assertEqual(payload["react_dev_command"], "run_web_gui_dev.bat")
        self.assertIn("npm run dev", payload["react_dev_manual_command"])
        self.assertIn("npm run build", payload["react_build_command"])
        self.assertEqual(payload["react_prod_command"], "run_web_gui_prod.bat")
        self.assertFalse(payload["frontend_build_available"])
        self.assertEqual(payload["observability"]["metrics_endpoint"], "/metrics")
        self.assertEqual(payload["observability"]["metrics_format"], "prometheus")
        self.assertEqual(payload["observability"]["request_logging"], "structured_json")
        self.assertIn("/metrics", payload["routes"]["GET"])
        self.assertIn("/api/state", payload["routes"]["GET"])
        self.assertIn("/api/markets/support-matrix", payload["routes"]["GET"])
        self.assertIn("/api/markets/{market_id}/support", payload["routes"]["GET"])
        self.assertIn("/api/live-safety", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/coverage", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/clob-readiness", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/live-validation", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/live-validation/reports", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/live-validation/reports/{key}", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/live-validation/reports/{key}/export.json", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/users/mdd/cache", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/users/mdd/cache/health", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/users/mdd/export.json", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/users/mdd/export.csv", payload["routes"]["GET"])
        self.assertIn("/api/config", payload["routes"]["PATCH"])
        self.assertIn("/api/live-safety/preflight", payload["routes"]["POST"])
        self.assertIn("/api/markets/{market_id}/orders/{operation}", payload["routes"]["POST"])
        self.assertNotIn("/api/markets/{market_id}/orders/{operation}", payload["routes"]["GET"])
        self.assertIn("/api/polymarket/users/mdd/cache/purge", payload["routes"]["POST"])
        self.assertIn("/api/polymarket/live-validation/reports", payload["routes"]["POST"])
        self.assertIn("/api/polymarket/users/mdd/cache/{key}", payload["routes"]["DELETE"])
        self.assertIn("/api/polymarket/live-validation/reports/{key}", payload["routes"]["DELETE"])

    def test_project_version_uses_distribution_metadata_then_source_metadata(self) -> None:
        with patch("web_api.importlib_metadata.version", return_value="2.3.4"):
            self.assertEqual(project_version(), "2.3.4")

        with patch(
            "web_api.importlib_metadata.version",
            side_effect=importlib_metadata.PackageNotFoundError,
        ):
            self.assertRegex(project_version(), r"^\d+\.\d+\.\d+")

    def test_polymarket_clob_readiness_payload_redacts_credentials(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "polymarket"
        cfg.markets["polymarket"].settings.update(
            {
                "private_key": "0x" + "1" * 64,
                "signature_type": 3,
                "funder_address": "0x" + "2" * 40,
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
            }
        )

        payload = polymarket_clob_readiness_payload(cfg)

        self.assertTrue(payload["selected"])
        self.assertTrue(payload["readiness"]["ok"])
        self.assertEqual(payload["readiness"]["private_key"]["redacted"], "***")
        self.assertEqual(payload["readiness"]["signature_type"]["name"], "POLY_1271")
        self.assertTrue(payload["live_safety"]["live_trading_enabled"])
        self.assertNotIn("1" * 64, str(payload))

    def test_polymarket_live_validation_payload_reports_stage_gates_without_live_actions(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "polymarket"
        cfg.markets["polymarket"].enabled = True
        cfg.markets["polymarket"].settings.update(
            {
                "private_key": "0x" + "1" * 64,
                "signature_type": 3,
                "funder_address": "0x" + "2" * 40,
            }
        )
        env = {
            "POLY_ADDRESS": "0xabc",
            "POLY_API_KEY": "key",
            "POLY_PASSPHRASE": "pass",
            "POLY_SIGNATURE": "sig",
            "POLY_TIMESTAMP": "1",
            "POLY_API_SECRET": "ws-secret",
        }

        with patch.dict(os.environ, env, clear=True):
            payload = polymarket_live_validation_payload(cfg)

        self.assertEqual(payload["mode"], "local_readiness_only")
        self.assertFalse(payload["funded_execution_exposed"])
        self.assertEqual(payload["credential_runbook"]["mode"], "credential_runbook_no_funded_actions")
        self.assertFalse(payload["credential_runbook"]["funded_execution_exposed"])
        self.assertIn("credentialed_read_no_funded_actions", payload["credential_runbook"]["operator_commands"])
        self.assertFalse(payload["stage_gates"]["credentialed_read_ok"])
        self.assertFalse(payload["stage_gates"]["safe_to_attempt_funded_order"])
        self.assertEqual(payload["authenticated_read_checks"]["clob_l2_orders"]["status"], "skipped")
        self.assertEqual(payload["authenticated_read_checks"]["user_websocket_auth_payload"]["status"], "skipped")
        self.assertIn("authenticated read", payload["stage_gates"]["next_step"])
        self.assertNotIn("1" * 64, str(payload))
        self.assertNotIn("ws-secret", str(payload))

    def test_polymarket_live_validation_reports_store_import_compare_and_redact(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "polymarket"
        cfg.markets["polymarket"].enabled = True
        cfg.markets["polymarket"].settings.update({"private_key": "0x" + "1" * 64})

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "reports.json"
            with patch.dict(os.environ, {"POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(report_path)}, clear=False):
                empty = polymarket_live_validation_reports_payload()
                self.assertEqual(empty["counts"]["entries"], 0)

                gui_payload = polymarket_live_validation_report_store_payload(cfg, {})
                gui_key = gui_payload["stored"]["key"]
                self.assertEqual(gui_payload["counts"]["entries"], 1)

                cli_report = {
                    "generated_at": 123.0,
                    "market_id": "polymarket",
                    "mode": "strict_cli",
                    "selected": True,
                    "enabled": True,
                    "api_key": "very-secret-api-key",
                    "private_key": "0x" + "9" * 64,
                    "clob_auth_readiness": {"direct_l2_read_ready": True, "sdk_trading_ready": True},
                    "stage_gates": {
                        "public_live_checks": "passed",
                        "credential_readiness": "passed",
                        "credentialed_read_checks": "passed",
                        "bridge_address_checks": "blocked",
                        "funded_live_order_check": "blocked",
                        "credentialed_read_ok": True,
                        "safe_to_attempt_funded_order": False,
                        "requires_explicit_live_approval": True,
                        "next_step": "funded order/cancel remains CLI-only",
                    },
                    "funded_execution_exposed": False,
                }

                imported = polymarket_live_validation_report_store_payload(
                    cfg,
                    {"report_json": json.dumps(cli_report), "label": "strict CLI read"},
                )

                self.assertEqual(imported["counts"]["entries"], 2)
                self.assertTrue(imported["stored"]["schema_validation"]["ok"])
                self.assertEqual(imported["stored"]["schema_validation"]["mode"], "strict_cli")
                self.assertIn("authenticated_read_checks is missing.", imported["stored"]["schema_validation"]["warnings"])
                self.assertEqual(imported["stored"]["summary"]["credential_readiness"], "passed")
                self.assertTrue(imported["stored"]["summary"]["credentialed_read_ok"])
                self.assertEqual(imported["stored"]["summary"]["credential_live_verified"], "blocked")
                self.assertFalse(imported["stored"]["summary"]["can_promote_credential_live_verified"])
                self.assertIn(
                    "no accepted authenticated-read evidence",
                    " ".join(imported["stored"]["summary"]["verification_promotion"]["blocked_reasons"]),
                )
                self.assertIsNotNone(imported["comparison"])
                self.assertTrue(report_path.exists())
                disk = report_path.read_text(encoding="utf-8")
                self.assertNotIn("very-secret-api-key", disk)
                self.assertNotIn("9" * 64, disk)
                self.assertIn("***", disk)
                opened = polymarket_live_validation_report_payload(imported["stored"]["key"])
                self.assertIsNotNone(opened)
                self.assertEqual(opened["entry"]["payload"]["api_key"], "***")
                self.assertTrue(opened["export"]["filename"].endswith(".json"))

                deleted = polymarket_live_validation_report_purge_payload({"key": gui_key})
                self.assertEqual(deleted["deleted"], 1)
                self.assertNotIn(gui_key, [entry["key"] for entry in deleted["entries"]])

    def test_polymarket_live_validation_report_api_skips_and_allows_duplicates(self) -> None:
        cfg = AppConfig()
        report = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "reports.json"
            with patch.dict(os.environ, {"POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(report_path)}, clear=False):
                first = polymarket_live_validation_report_store_payload(
                    cfg,
                    {
                        "report_json": json.dumps(report),
                        "label": "dry run",
                        "source": "cli_import",
                        "source_file": "valid_dry_run.json",
                    },
                )
                self.assertEqual(first["counts"]["entries"], 1)
                self.assertTrue(first["stored"]["stored"])
                self.assertEqual(len(first["stored"]["payload_hash"]), 64)
                self.assertEqual(first["stored"]["provenance"]["source_file_name"], "valid_dry_run.json")

                skipped = polymarket_live_validation_report_store_payload(
                    cfg,
                    {
                        "report_json": json.dumps(report),
                        "label": "dry run replay",
                        "source": "cli_import",
                        "source_file": "valid_dry_run-copy.json",
                    },
                )
                self.assertEqual(skipped["counts"]["entries"], 1)
                self.assertFalse(skipped["stored"]["stored"])
                self.assertTrue(skipped["stored"]["duplicate"])
                self.assertEqual(skipped["stored"]["duplicate_key"], first["stored"]["key"])
                self.assertIn("Skipped duplicate", skipped["message"])
                self.assertEqual(skipped["counts"]["duplicate_imports"], 1)

                allowed = polymarket_live_validation_report_store_payload(
                    cfg,
                    {
                        "report_json": json.dumps(report),
                        "label": "dry run duplicate evidence",
                        "source": "cli_import",
                        "source_file": "valid_dry_run-allowed.json",
                        "allow_duplicate": True,
                    },
                )
                self.assertEqual(allowed["counts"]["entries"], 2)
                self.assertTrue(allowed["stored"]["stored"])
                self.assertTrue(allowed["stored"]["duplicate"])
                self.assertEqual(allowed["stored"]["duplicate_of"], first["stored"]["key"])
                self.assertIn("Stored duplicate", allowed["message"])

    def test_polymarket_live_validation_report_routes_open_export_and_delete(self) -> None:
        report = {
            "generated_at": 123.0,
            "market_id": "polymarket",
            "mode": "strict_cli",
            "api_key": "route-secret",
            "stage_gates": {
                "public_live_checks": "passed",
                "credential_readiness": "passed",
                "credentialed_read_checks": "blocked",
                "bridge_address_checks": "blocked",
                "funded_live_order_check": "blocked",
                "credentialed_read_ok": False,
                "safe_to_attempt_funded_order": False,
                "requires_explicit_live_approval": True,
                "next_step": "authenticated read required",
            },
            "funded_execution_exposed": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            report_path = Path(tmpdir) / "live-reports.json"
            decision_path = Path(tmpdir) / "live-decisions.json"
            snapshot_path = Path(tmpdir) / "live-proposal-snapshots.json"
            with patch.dict(
                os.environ,
                {
                    "POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(report_path),
                    "POLYMARKET_LIVE_VALIDATION_DECISIONS_PATH": str(decision_path),
                    "POLYMARKET_LIVE_VALIDATION_PROMOTION_PROPOSAL_SNAPSHOTS_PATH": str(snapshot_path),
                },
                clear=False,
            ):
                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    status, stored = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/reports",
                        method="POST",
                        payload={"report_json": json.dumps(report), "label": "route report"},
                    )
                    self.assertEqual(status, 200)
                    report_key = stored["stored"]["key"]

                    status, listing = self._request_json(base_url, "/api/polymarket/live-validation/reports")
                    self.assertEqual(status, 200)
                    self.assertEqual(listing["counts"]["entries"], 1)

                    status, opened = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(opened["entry"]["payload"]["api_key"], "***")
                    self.assertEqual(opened["entry"]["summary"]["credential_readiness"], "passed")
                    self.assertEqual(opened["entry"]["summary"]["credential_live_verified"], "blocked")
                    self.assertTrue(opened["entry"]["schema_validation"]["ok"])
                    self.assertEqual(opened["entry"]["schema_validation"]["mode"], "strict_cli")

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}/export.json",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("attachment", headers.get("Content-Disposition", ""))
                    exported = json.loads(body.decode("utf-8"))
                    self.assertEqual(exported["entry"]["payload"]["api_key"], "***")
                    self.assertTrue(exported["entry"]["schema_validation"]["ok"])
                    self.assertEqual(exported["entry"]["schema_validation"]["mode"], "strict_cli")
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    direct_review = polymarket_live_validation_report_review_payload(report_key)
                    self.assertIsNotNone(direct_review)
                    self.assertEqual(direct_review["bundle"]["report"]["key"], report_key)
                    self.assertFalse(direct_review["bundle"]["static_coverage_mutated"])

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}/review.json",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("attachment", headers.get("Content-Disposition", ""))
                    review_json = json.loads(body.decode("utf-8"))
                    self.assertEqual(review_json["bundle"]["source"], "polymarket_live_validation_report_review_bundle")
                    self.assertEqual(review_json["bundle"]["report"]["key"], report_key)
                    self.assertEqual(review_json["bundle"]["promotion_review"]["credential_live_verified"], "blocked")
                    self.assertFalse(review_json["bundle"]["coverage_tier_mapping"]["static_coverage_mutated"])
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    decision_request = {
                        "report_key": report_key,
                        "payload_hash": review_json["bundle"]["report"]["payload_hash"],
                        "target_tier": "credential_live_verified",
                        "decision": "rejected",
                        "reviewer": "route-test",
                        "reviewer_note": "Route report has no accepted credential evidence.",
                        "review_bundle_hash": review_json["bundle"]["review_bundle_hash"],
                    }
                    direct_decision = polymarket_live_validation_decision_store_payload(decision_request)
                    self.assertEqual(direct_decision["counts"]["entries"], 1)
                    self.assertFalse(direct_decision["stored"]["static_coverage_mutated"])

                    status, decision = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/decisions",
                        method="POST",
                        payload={**decision_request, "reviewer_note": "Second rejected route decision."},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(decision["counts"]["entries"], 2)
                    self.assertEqual(decision["stored"]["decision"], "rejected")
                    self.assertTrue(decision["stored"]["review_bundle_hash_verified"])
                    self.assertFalse(decision["stored"]["static_coverage_mutated"])

                    status, ledger = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/decisions?report_key={report_key}",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(ledger["counts"]["entries"], 2)
                    self.assertEqual(polymarket_live_validation_decisions_payload()["counts"]["entries"], 2)

                    status, headers, body = self._request_raw(
                        base_url,
                        "/api/polymarket/live-validation/decisions/export.json",
                    )
                    self.assertEqual(status, 200)
                    ledger_export = json.loads(body.decode("utf-8"))
                    self.assertEqual(ledger_export["counts"]["entries"], 2)
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    status, headers, body = self._request_raw(
                        base_url,
                        "/api/polymarket/live-validation/decisions/export.md",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("Promotion Decision Ledger", body.decode("utf-8"))
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    direct_proposal = polymarket_live_validation_promotion_proposal_payload()
                    self.assertEqual(direct_proposal["counts"]["ledger_entries"], 2)
                    self.assertEqual(direct_proposal["counts"]["accepted_candidates"], 0)
                    self.assertFalse(direct_proposal["automerge_enabled"])
                    self.assertFalse(direct_proposal["static_coverage_mutated"])

                    status, proposal = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/promotion-proposal",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(proposal["counts"]["ignored_decisions"], 2)
                    self.assertTrue(proposal["human_review_required"])
                    self.assertFalse(proposal["apply_by_default"])

                    status, headers, body = self._request_raw(
                        base_url,
                        "/api/polymarket/live-validation/promotion-proposal/export.json",
                    )
                    self.assertEqual(status, 200)
                    proposal_export = json.loads(body.decode("utf-8"))
                    self.assertEqual(proposal_export["source"], "polymarket_live_validation_coverage_promotion_proposal")
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    status, headers, body = self._request_raw(
                        base_url,
                        "/api/polymarket/live-validation/promotion-proposal/export.md",
                    )
                    self.assertEqual(status, 200)
                    proposal_markdown = body.decode("utf-8")
                    self.assertIn("Coverage Promotion Proposal", proposal_markdown)
                    self.assertIn("Automerge enabled: false", proposal_markdown)
                    self.assertNotIn("route-secret", proposal_markdown)

                    direct_snapshot = polymarket_live_validation_promotion_proposal_snapshot_store_payload(
                        {"target_tier": "credential_live_verified", "source": "route-test"}
                    )
                    self.assertEqual(direct_snapshot["counts"]["entries"], 1)
                    self.assertFalse(direct_snapshot["stored"]["static_coverage_mutated"])
                    self.assertEqual(polymarket_live_validation_promotion_proposal_snapshots_payload()["counts"]["entries"], 1)

                    status, snapshots = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/promotion-proposal/snapshots",
                        method="POST",
                        payload={"target_tier": "credential_live_verified", "source": "route-test"},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(snapshots["counts"]["entries"], 2)
                    snapshot_key = snapshots["stored"]["key"]

                    direct_opened = polymarket_live_validation_promotion_proposal_snapshot_payload(snapshot_key)
                    self.assertIsNotNone(direct_opened)
                    self.assertEqual(direct_opened["entry"]["key"], snapshot_key)
                    self.assertFalse(direct_opened["entry"]["static_coverage_mutated"])
                    direct_diff = polymarket_live_validation_promotion_proposal_snapshot_diff_payload(snapshot_key)
                    self.assertIsNotNone(direct_diff)
                    assert direct_diff is not None
                    self.assertFalse(direct_diff["static_coverage_mutated"])

                    status, opened_snapshot = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn(opened_snapshot["entry"]["snapshot_status"], {"current", "stale"})
                    self.assertIn("diff", opened_snapshot)
                    self.assertNotIn("route-secret", json.dumps(opened_snapshot, sort_keys=True))

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/export.json",
                    )
                    self.assertEqual(status, 200)
                    snapshot_export = json.loads(body.decode("utf-8"))
                    self.assertEqual(snapshot_export["entry"]["key"], snapshot_key)
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/diff.json",
                    )
                    self.assertEqual(status, 200)
                    snapshot_diff = json.loads(body.decode("utf-8"))
                    self.assertEqual(snapshot_diff["snapshot_key"], snapshot_key)
                    self.assertFalse(snapshot_diff["static_coverage_mutated"])
                    self.assertNotIn("route-secret", body.decode("utf-8"))

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/diff.md",
                    )
                    self.assertEqual(status, 200)
                    snapshot_diff_markdown = body.decode("utf-8")
                    self.assertIn("Current-vs-Snapshot Diff", snapshot_diff_markdown)
                    self.assertNotIn("route-secret", snapshot_diff_markdown)

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}/export.md",
                    )
                    self.assertEqual(status, 200)
                    snapshot_markdown = body.decode("utf-8")
                    self.assertIn("Promotion Proposal Snapshot", snapshot_markdown)
                    self.assertNotIn("route-secret", snapshot_markdown)

                    status, deleted_snapshot = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/promotion-proposal/snapshots/{snapshot_key}",
                        method="DELETE",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(deleted_snapshot["deleted"], 1)

                    status, headers, body = self._request_raw(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}/review.md",
                    )
                    self.assertEqual(status, 200)
                    self.assertIn("text/markdown", headers.get("Content-Type", ""))
                    review_markdown = body.decode("utf-8")
                    self.assertIn("Polymarket Live Validation Review Bundle", review_markdown)
                    self.assertIn("Static coverage mutated: false", review_markdown)
                    self.assertIn("Coverage Tier Mapping", review_markdown)
                    self.assertNotIn("route-secret", review_markdown)

                    status, deleted = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}",
                        method="DELETE",
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(deleted["deleted"], 1)

                    status, missing = self._request_json(
                        base_url,
                        f"/api/polymarket/live-validation/reports/{report_key}",
                    )
                    self.assertEqual(status, 404)
                    self.assertEqual(missing["error"]["code"], "not_found")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_polymarket_live_validation_report_route_returns_schema_error_without_storing(self) -> None:
        invalid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "invalid_missing_mode.json").read_text(encoding="utf-8"))
        valid = json.loads((LIVE_REPORT_FIXTURE_ROOT / "valid_dry_run.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            report_path = Path(tmpdir) / "live-reports.json"
            with patch.dict(os.environ, {"POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(report_path)}, clear=False):
                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    status, failed = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/reports",
                        method="POST",
                        payload={"report_json": json.dumps(invalid), "label": "bad fixture"},
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(failed["error"]["code"], "live_validation_report_schema_error")
                    validation = failed["error"]["details"]["schema_validation"]
                    self.assertFalse(validation["ok"])
                    self.assertIn("Live validation report requires", " ".join(validation["errors"]))
                    self.assertIn("strict_cli", validation["accepted_modes"])

                    status, listing = self._request_json(base_url, "/api/polymarket/live-validation/reports")
                    self.assertEqual(status, 200)
                    self.assertEqual(listing["counts"]["entries"], 0)

                    status, stored = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/reports",
                        method="POST",
                        payload={"report_json": json.dumps(valid), "label": "valid dry-run fixture"},
                    )
                    self.assertEqual(status, 200)
                    self.assertTrue(stored["stored"]["schema_validation"]["ok"])
                    self.assertEqual(stored["stored"]["schema_validation"]["mode"], "strict_cli")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_polymarket_coverage_route_includes_guarded_report_promotion_inventory(self) -> None:
        report = {
            "generated_at": 123.0,
            "market_id": "polymarket",
            "mode": "strict_cli",
            "authenticated_read_checks": {
                "user_websocket_connect": {"status": "ok", "detail": "connected", "sample_type": "dict"}
            },
            "funded_live_order_check": {"status": "dry_run", "live_action": False},
            "stage_gates": {
                "credentialed_read_ok": True,
                "credentialed_read_checks": "ok",
                "funded_live_order_check": "dry_run",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            report_path = Path(tmpdir) / "live-reports.json"
            with patch.dict(os.environ, {"POLYMARKET_LIVE_VALIDATION_REPORTS_PATH": str(report_path)}, clear=False):
                server, thread, base_url = self._serve_api(config_path, frontend_dir)
                try:
                    status, _stored = self._request_json(
                        base_url,
                        "/api/polymarket/live-validation/reports",
                        method="POST",
                        payload={"report_json": json.dumps(report), "label": "credentialed read"},
                    )
                    self.assertEqual(status, 200)

                    status, coverage = self._request_json(base_url, "/api/polymarket/coverage")
                    self.assertEqual(status, 200)
                    promotion = coverage["stored_live_validation_report_promotion"]
                    self.assertFalse(promotion["static_coverage_mutated"])
                    self.assertEqual(promotion["credential_live_verified"], "yes")
                    self.assertEqual(promotion["funded_live_verified"], "blocked")
                    self.assertEqual(promotion["counts"]["credential_candidates"], 1)
                    authenticated_category = [
                        item for item in coverage["categories"] if item["name"] == "CLOB authenticated trading and account data"
                    ][0]
                    self.assertEqual(authenticated_category["coverage_levels"]["credential_live_verified"], "blocked")
                    self.assertEqual(authenticated_category["coverage_levels"]["funded_live_verified"], "blocked")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_api_error_payload_uses_structured_shape_and_redacts_detail_keys(self) -> None:
        payload = api_error_payload(
            400,
            "validation_error",
            "Invalid payload.",
            {"api_key": "super-secret-key", "field": "theme"},
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "validation_error")
        self.assertEqual(payload["error"]["status"], 400)
        self.assertEqual(payload["error"]["message"], "Invalid payload.")
        self.assertEqual(payload["error"]["details"]["api_key"], "***")
        self.assertNotIn("super-secret-key", json.dumps(payload))

    def test_json_body_reader_rejects_bad_shape_size_and_encoding(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON request body must be an object"):
            _read_json_body(FakeBodyHandler(b"[]"))
        with self.assertRaisesRegex(ValueError, "JSON request body is too large"):
            _read_json_body(FakeBodyHandler(b"{}", "1000001"))
        with self.assertRaisesRegex(ValueError, "Content-Length must be an integer"):
            _read_json_body(FakeBodyHandler(b"{}", "bad"))
        with self.assertRaisesRegex(ValueError, "JSON request body must be UTF-8"):
            _read_json_body(FakeBodyHandler(b"\xff"))

    def test_http_mutation_errors_use_structured_response_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            server, thread, base_url = self._serve_api(config_path, frontend_dir)
            try:
                status, invalid_json = self._request_json(
                    base_url,
                    "/api/config",
                    method="PATCH",
                    raw=b"{not-json",
                    headers={"Content-Type": "application/json"},
                )
                status_validation, validation = self._request_json(
                    base_url,
                    "/api/config",
                    method="PATCH",
                    payload={"theme": "blue"},
                )
                status_not_found, not_found = self._request_json(base_url, "/api/missing")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(status, 400)
        self.assertEqual(invalid_json["error"]["code"], "invalid_json")
        self.assertFalse(invalid_json["ok"])
        self.assertEqual(status_validation, 400)
        self.assertEqual(validation["error"]["code"], "validation_error")
        self.assertEqual(validation["error"]["message"], "theme must be light or dark.")
        self.assertEqual(status_not_found, 404)
        self.assertEqual(not_found["error"]["code"], "not_found")

    def test_http_static_route_reports_missing_react_build_with_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            server, thread, base_url = self._serve_api(config_path, frontend_dir)
            try:
                status, payload = self._request_json(base_url, "/")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "react_build_missing")
        self.assertIn("npm run build", payload["error"]["details"]["build_command"])
        self.assertEqual(payload["error"]["details"]["dev_command"], "run_web_gui_dev.bat")
        self.assertIn("run_gui.bat", payload["error"]["details"]["tkinter_fallback"])

    def test_http_static_route_serves_built_react_assets_and_spa_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            frontend_dir = Path(tmpdir) / "dist"
            asset_dir = frontend_dir / "assets"
            asset_dir.mkdir(parents=True)
            (frontend_dir / "index.html").write_text("<html><body>React app</body></html>", encoding="utf-8")
            (asset_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")
            (asset_dir / "app-abcdefgh.js").write_text("console.log('immutable');", encoding="utf-8")

            server, thread, base_url = self._serve_api(config_path, frontend_dir)
            try:
                root_status, root_headers, root_body = self._request_raw(base_url, "/")
                asset_status, asset_headers, asset_body = self._request_raw(base_url, "/assets/app.js")
                hashed_asset_status, hashed_asset_headers, hashed_asset_body = self._request_raw(
                    base_url, "/assets/app-abcdefgh.js"
                )
                fallback_status, fallback_headers, fallback_body = self._request_raw(base_url, "/settings/live-safety")
                traversal_status, _traversal_headers, traversal_body = self._request_raw(base_url, "/%2e%2e/README.md")
                health_status, health_headers, health_body = self._request_raw(base_url, "/api/health")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(root_status, 200)
        expected_security_headers = {
            "Content-Security-Policy": "default-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=()",
            "Cross-Origin-Opener-Policy": "same-origin",
        }
        for headers in (root_headers, asset_headers, hashed_asset_headers, fallback_headers, health_headers):
            with self.subTest(headers=headers.get("Content-Type")):
                for name, value in expected_security_headers.items():
                    self.assertIn(value, headers.get(name, ""))
                self.assertEqual(headers.get("Server"), "MarketSentinel")
        self.assertIn("text/html", root_headers["Content-Type"])
        self.assertEqual(root_headers.get("Cache-Control"), "no-store")
        self.assertIn(b"React app", root_body)
        self.assertEqual(asset_status, 200)
        self.assertIn("javascript", asset_headers["Content-Type"])
        self.assertEqual(asset_headers.get("Cache-Control"), "no-cache, max-age=0, must-revalidate")
        self.assertEqual(asset_body, b"console.log('ok');")
        self.assertEqual(hashed_asset_status, 200)
        self.assertEqual(hashed_asset_headers.get("Cache-Control"), "public, max-age=31536000, immutable")
        self.assertEqual(hashed_asset_body, b"console.log('immutable');")
        self.assertEqual(fallback_status, 200)
        self.assertEqual(fallback_headers.get("Cache-Control"), "no-store")
        self.assertIn(b"React app", fallback_body)
        self.assertEqual(traversal_status, 200)
        self.assertIn(b"React app", traversal_body)
        self.assertEqual(health_status, 200)
        self.assertEqual(health_headers.get("Cache-Control"), "no-store")
        health = json.loads(health_body.decode("utf-8"))
        self.assertTrue(health["frontend_build_available"])

    def test_static_cache_control_rejects_unknown_relative_path(self) -> None:
        self.assertEqual(static_cache_control(None), "no-store")

    def test_static_path_resolution_rejects_encoded_windows_separator_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frontend_dir = root / "dist"
            frontend_dir.mkdir()
            (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            asset_dir = frontend_dir / "assets"
            asset_dir.mkdir()
            asset = asset_dir / "app.js"
            asset.write_text("console.log('ok');", encoding="utf-8")
            with patch("web_api.DEFAULT_FRONTEND_DIR", frontend_dir):
                static_files = ReactGuiHandler._static_file_catalog()

            self.assertIsNone(
                ReactGuiHandler._resolve_static_path(None, static_files, "/%2e%2e%5coutside.txt")
            )
            self.assertIsNone(ReactGuiHandler._resolve_static_path(None, static_files, "/assets/nested/app.js"))
            self.assertEqual(
                ReactGuiHandler._resolve_static_path(None, static_files, "/"),
                (frontend_dir / "index.html").resolve(),
            )
            self.assertEqual(
                ReactGuiHandler._resolve_static_path(None, static_files, "/assets/app.js"),
                asset.resolve(),
            )

    def test_static_file_catalog_uses_packaged_frontend_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            frontend_dir = Path(tmpdir) / "dist"
            frontend_dir.mkdir()
            index = frontend_dir / "index.html"
            index.write_text("<html></html>", encoding="utf-8")

            with patch("web_api.DEFAULT_FRONTEND_DIR", frontend_dir):
                static_files = ReactGuiHandler._static_file_catalog()

        self.assertEqual(static_files, {"index.html": index.resolve()})

    def test_app_state_payload_combines_initial_react_gui_state(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        cfg.markets["kalshi"].enabled = True
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            )
        ]

        payload = app_state_payload(cfg, Path("local-config.json"), Path("frontend-dist"))

        self.assertEqual(payload["health"]["mode"], "parallel")
        self.assertEqual(payload["config"]["selected_market_id"], "kalshi")
        self.assertEqual(payload["markets"]["selected_market_id"], "kalshi")
        self.assertEqual(payload["live_safety"]["selected_market_id"], "kalshi")
        self.assertEqual(payload["paper"]["summary"]["positions"], 1)

    def test_http_state_route_reads_config_file(self) -> None:
        cfg = AppConfig()
        cfg.selected_market_id = "kalshi"
        cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="accepted",
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            save_config(cfg, config_path)
            payload = app_state_payload(
                load_config(config_path),
                config_path,
                Path(tmpdir) / "dist",
            )

        self.assertEqual(payload["health"]["status"], "ok")
        self.assertEqual(payload["config"]["selected_market_id"], "kalshi")
        self.assertEqual(payload["paper"]["counts"]["history"], 1)

    def test_http_support_matrix_routes_return_full_catalog_and_single_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            frontend_dir = root / "dist"
            frontend_dir.mkdir()
            (frontend_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            server, thread, base_url = self._serve_api(root / "config.json", frontend_dir)
            try:
                status, payload = self._request_json(base_url, "/api/markets/support-matrix")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(len(payload["markets"]), 68)
                self.assertEqual(payload["counts"]["total"], 68)

                status, row_payload = self._request_json(base_url, "/api/markets/kalshi/support")
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(row_payload["market"]["market_id"], "kalshi")
                self.assertEqual(row_payload["markets"], [row_payload["market"]])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
