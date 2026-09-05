from __future__ import annotations

import io
import csv
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from importlib import metadata as importlib_metadata
from pathlib import Path
from unittest.mock import patch

from app import (
    App,
    AdapterPricePoller,
    CopyActivityOutcome,
    WalletActivityTask,
    WalletPoller,
    _COPY_ACTIVITY_MAX_REPLAY_AGE_SECONDS,
    _apply_wallet_activity_checkpoint,
    _copy_execution_policy,
    activity_key,
    application_resource_roots,
    extract_slug,
    market_choice_label,
    market_id_from_choice,
    main,
    safe_float,
    tkinter_gui_lifecycle_smoke_payload,
    tkinter_smoke_payload,
)
from core.models import (
    AppConfig,
    CopyActivityOutboxEntry,
    CopyTradeSettings,
    PaperTradeRecord,
    PriceAlert,
    WalletWatch,
)
from core.storage import ConfigLoadError
from market_adapters import MARKET_IDS, build_default_registry
from market_adapters.errors import MarketConfigurationError
from market_adapters.types import (
    MarketCapabilities,
    MarketContract,
    MarketEvent,
    MarketMetadata,
    OrderBookLevel,
    OrderBookSnapshot,
    PaperOrderRequest,
    PaperOrderResult,
    PriceSnapshot,
)


WALLET = "0x" + "b" * 40


class FakeVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeEntry(FakeVar):
    def insert(self, _index: int, value: str) -> None:
        self.value = value


class FakeListbox:
    def __init__(self) -> None:
        self.items = []
        self.selection = ()

    def delete(self, *_args) -> None:
        self.items = []

    def insert(self, _index, value: str) -> None:
        self.items.append(value)

    def curselection(self):
        return self.selection


class FakeButton:
    def __init__(self) -> None:
        self.state = "normal"

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]


class FakeTree:
    def __init__(self) -> None:
        self.rows = {}
        self.selection_ids = ()
        self.next_id = 0

    def get_children(self):
        return list(self.rows)

    def delete(self, iid) -> None:
        self.rows.pop(iid, None)

    def insert(self, _parent, _index, iid=None, values=()) -> None:
        if iid is None:
            iid = f"row-{self.next_id}"
            self.next_id += 1
        self.rows[iid] = values

    def selection(self):
        return self.selection_ids

    def selection_set(self, iid) -> None:
        self.selection_ids = (iid,)

    def item(self, iid, option=None):
        values = self.rows.get(iid, ())
        if option == "values":
            return values
        return {"values": values}


class MarketSelectionHarness:
    def __init__(self) -> None:
        self.cfg = AppConfig()
        self.adapter_registry = build_default_registry()
        self.market_var = FakeVar()
        self.status_var = FakeVar()
        self.market_status_var = FakeVar()
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()

    def _market_label_for_id(self, market_id: str) -> str:
        return App._market_label_for_id(self, market_id)

    def _get_selected_market_adapter(self):
        return App._get_selected_market_adapter(self)

    def _selected_market_display_name(self) -> str:
        return App._selected_market_display_name(self)

    def _selected_market_status_text(self, adapter=None) -> str:
        return App._selected_market_status_text(self, adapter)


class SafetyHarness(MarketSelectionHarness):
    def __init__(self) -> None:
        super().__init__()
        self.cfg.selected_market_id = "kalshi"
        self.safety_market_var = FakeVar()
        self.safety_market_enabled_var = FakeVar(False)
        self.safety_live_enabled_var = FakeVar(False)
        self.safety_live_confirmed_var = FakeVar(False)
        self.safety_kill_switch_var = FakeVar(False)
        self.safety_max_size_var = FakeVar("")
        self.safety_max_notional_var = FakeVar("")
        self.safety_status_var = FakeVar()
        self.safety_tree = FakeTree()


class AlertHarness:
    def __init__(self, alert: PriceAlert, value: float) -> None:
        self.cfg = AppConfig(alerts=[alert])
        self.price_state = {alert.token_id: {alert.source: value}}
        self.fired = []
        self.refreshed = False

    def _fire_alert(self, alert: PriceAlert, value: float) -> None:
        self.fired.append((alert.id, value))

    def _refresh_alert_table(self) -> None:
        self.refreshed = True


class MultiAlertHarness:
    def __init__(self, alerts, price_state) -> None:
        self.cfg = AppConfig(alerts=list(alerts))
        self.price_state = price_state
        self.fired = []
        self.refreshed = False

    def _fire_alert(self, alert: PriceAlert, value: float) -> None:
        self.fired.append((alert.market_id, alert.token_id, value))

    def _refresh_alert_table(self) -> None:
        self.refreshed = True


class CopyHarness:
    def __init__(self) -> None:
        self.cfg = AppConfig(
            copytrading=CopyTradeSettings(
                enabled=True,
                live=False,
                follow_wallet=WALLET,
                follow_wallets=[WALLET],
                scale=1.0,
                max_usdc_per_trade=5.0,
                slippage=0.02,
            )
        )
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self._geoblock_cache = None
        self._copy_conflict_cache = {}
        self.polymarket_adapter = FakePolymarketAdapter()

    def _get_polymarket_adapter(self) -> "FakePolymarketAdapter":
        return self.polymarket_adapter


class AnalyticsHarness:
    def __init__(self) -> None:
        self.cfg = AppConfig()
        self.lb_sort_var = FakeVar("ROI %")
        self.lb_direction_var = FakeVar("High to low")
        self.lb_limit_var = FakeVar("1000")
        self.lb_scan_limit_var = FakeVar("1000")
        self.lb_period_var = FakeVar("All")
        self.lb_category_var = FakeVar("OVERALL")
        self.lb_compute_mdd_var = FakeVar(False)
        self.lb_fast_scan_var = FakeVar(True)
        self.lb_mdd_mode_var = FakeVar("Fast public curve")
        self.lb_mdd_scan_limit_var = FakeVar("100")
        self.lb_min_roi_var = FakeVar("")
        self.lb_max_roi_var = FakeVar("")
        self.lb_min_mdd_pct_var = FakeVar("")
        self.lb_max_mdd_pct_var = FakeVar("")
        self.leaderboard_tree = FakeTree()
        self.wallet_tree = FakeTree()
        self.lb_returned_metric_var = FakeVar()
        self.lb_scanned_metric_var = FakeVar()
        self.lb_best_roi_metric_var = FakeVar()
        self.lb_mdd_metric_var = FakeVar()
        self.lb_status_var = FakeVar()
        self.status_var = FakeVar()
        self.ct_follow_var = FakeVar("")
        self.lb_fast_roi_btn = FakeButton()
        self.lb_cancel_btn = FakeButton()
        self._leaderboard_loading = False
        self._leaderboard_cancel_event = threading.Event()
        self._leaderboard_row_by_iid = {}
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.logged = []
        self.clipboard = []

    def log(self, message: str) -> None:
        self.logged.append(message)

    def clipboard_clear(self) -> None:
        self.clipboard = []

    def clipboard_append(self, value: str) -> None:
        self.clipboard.append(value)

    def update_idletasks(self) -> None:
        pass

    def _selected_leaderboard_row(self):
        return App._selected_leaderboard_row(self)

    def _selected_leaderboard_wallet(self):
        return App._selected_leaderboard_wallet(self)

    def _selected_leaderboard_display_name(self):
        return App._selected_leaderboard_display_name(self)

    def _copy_text_to_clipboard(self, text: str, label: str):
        return App._copy_text_to_clipboard(self, text, label)

    def _ensure_wallet_watch_from_leaderboard(self, wallet: str, display_name: str = "", *, persist: bool = True):
        return App._ensure_wallet_watch_from_leaderboard(self, wallet, display_name, persist=persist)

    def _copy_follow_wallets_from_text(self):
        return App._copy_follow_wallets_from_text(self)

    def _refresh_wallet_table(self):
        return App._refresh_wallet_table(self)


class FakePolymarketAdapter:
    def __init__(self) -> None:
        self.preflight_calls = []
        self.preflight_error = None
        self.geoblock_result = {"blocked": False, "country": "US", "region": "test"}
        self.geoblock_error = None

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            market_id="polymarket",
            contract_id=contract_id,
            bids=[OrderBookLevel(price=0.48, size=10.0)],
            asks=[OrderBookLevel(price=0.50, size=10.0)],
        )

    def preflight_live_order(self, order, *, feature_name: str = "live trading"):
        self.preflight_calls.append((order, feature_name))
        if self.preflight_error:
            raise self.preflight_error
        return {"approx_notional": float(order.size) * float(order.limit_price or 1.0)}

    def check_geoblock(self):
        if self.geoblock_error:
            raise self.geoblock_error
        return self.geoblock_result


class FakeOpinionActivityAdapter:
    market_id = "opinion_labs"
    display_name = "Opinion Labs"
    capabilities = MarketCapabilities(
        orderbook_reading=True,
        paper_trading=True,
        copy_trading=True,
    )

    def __init__(self) -> None:
        self.activity_calls = []
        self.paper_orders = []
        self.stopper = None

    def list_activity(self, wallet: str, *, limit: int = 25):
        self.activity_calls.append((wallet, limit))
        if len(self.activity_calls) >= 2 and self.stopper:
            self.stopper()
        return [
            {
                "timestamp": 100,
                "transactionHash": "opinion-tx-1",
                "proxyWallet": wallet,
                "asset": "77:YES:0xyes",
                "side": "BUY",
                "size": 2,
                "price": 0.5,
                "slug": "77",
            }
        ]

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            market_id=self.market_id,
            contract_id=contract_id,
            bids=[OrderBookLevel(price=0.48, size=10.0)],
            asks=[OrderBookLevel(price=0.50, size=10.0)],
        )

    def place_paper_order(self, order: PaperOrderRequest) -> PaperOrderResult:
        self.paper_orders.append(order)
        return PaperOrderResult(
            market_id=order.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message="DRY RUN accepted",
            filled_size=order.size,
            average_price=order.limit_price,
        )


class FakePriceAdapter:
    market_id = "kalshi"
    display_name = "Kalshi"
    capabilities = MarketCapabilities(price_reading=True)

    def __init__(self) -> None:
        self.contracts = []

    def get_price(self, contract_id: str) -> PriceSnapshot:
        self.contracts.append(contract_id)
        return PriceSnapshot(
            market_id="kalshi",
            contract_id=contract_id,
            last=0.62,
            bid=0.60,
            ask=0.64,
            source="unit-test",
        )


class FakePaperAdapter(FakePriceAdapter):
    capabilities = MarketCapabilities(price_reading=True, paper_trading=True)

    def __init__(self) -> None:
        super().__init__()
        self.orders = []

    def place_paper_order(self, order):
        self.orders.append(order)
        return PaperOrderResult(
            market_id=order.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message="DRY RUN accepted",
            filled_size=order.size,
            average_price=order.limit_price,
            raw={"request": {"contract_id": order.contract_id}},
        )


class FakeQuoteAdapter(FakePaperAdapter):
    capabilities = MarketCapabilities(price_reading=True, orderbook_reading=True, paper_trading=True)

    def __init__(self) -> None:
        super().__init__()
        self.orderbooks = []

    def get_orderbook(self, contract_id: str) -> OrderBookSnapshot:
        self.orderbooks.append(contract_id)
        return OrderBookSnapshot(
            market_id="kalshi",
            contract_id=contract_id,
            bids=[OrderBookLevel(price=0.58, size=12.0)],
            asks=[OrderBookLevel(price=0.66, size=15.0)],
        )


class FakeLiveAdapter(FakePaperAdapter):
    market_id = "kalshi"
    display_name = "Kalshi"
    capabilities = MarketCapabilities(price_reading=True, paper_trading=True, live_trading=True)

    def __init__(self, *, error=None) -> None:
        super().__init__()
        self.error = error
        self.preflight_orders = []

    def preflight_live_order(self, order, *, feature_name: str = "live trading"):
        self.preflight_orders.append((order, feature_name))
        if self.error:
            raise self.error
        return {
            "dry_run_preview": f"Would submit live {order.side} order for {order.size:g}",
            "approx_notional": float(order.size) * float(order.limit_price or 1.0),
            "max_notional": 10.0,
            "warnings": ["credentials_required"],
        }


class FakeRegistry:
    def __init__(self, adapter: FakePriceAdapter) -> None:
        self.adapter = adapter
        self.calls = []

    def create(self, market_id: str, settings=None) -> FakePriceAdapter:
        self.calls.append((market_id, settings or {}))
        return self.adapter


class AppLogicTests(unittest.TestCase):
    def test_frozen_smoke_payload_uses_packaged_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "market-sentinel-v1.0.11-win-x64"
            (package_root / "assets").mkdir(parents=True)
            (package_root / "assets" / "marketsentinel.ico").write_bytes(b"ico")
            (package_root / "assets" / "marketsentinel.png").write_bytes(b"png")
            executable = package_root / "market-sentinel.exe"
            executable.write_bytes(b"exe")

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
            ):
                # Windows runners can canonicalize temporary paths to a short
                # name or normalized casing when resolving the executable.
                self.assertEqual(application_resource_roots()[0], package_root.resolve())
                self.assertTrue(tkinter_smoke_payload()["icon_available"])

    def test_release_asset_helpers_prefer_packaged_icons_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "market-sentinel-v1.0.12-win-x64"
            source_root = Path(tmp) / "source-checkout"
            package_icons = package_root / "assets" / "icons"
            source_icons = source_root / "assets" / "icons"
            package_icons.mkdir(parents=True)
            source_icons.mkdir(parents=True)
            package_icon = package_root / "assets" / "marketsentinel.ico"
            package_icon.write_bytes(b"ico")
            package_png = package_icons / "marketsentinel-64.png"
            package_png.write_bytes(b"png")
            (source_root / "assets" / "marketsentinel.png").write_bytes(b"fallback-png")
            (source_icons / "marketsentinel-256.png").write_bytes(b"source-png")
            package_requirements = package_root / "requirements.txt"
            package_requirements.write_text("requests==2.32.3\n", encoding="utf-8")
            package_pyproject = package_root / "pyproject.toml"
            package_pyproject.write_text("[project]\nname = 'market-sentinel'\n", encoding="utf-8")

            class ReleaseAssetHarness:
                def _resource_roots(self) -> list[Path]:
                    return [package_root, source_root]

            harness = ReleaseAssetHarness()

            self.assertEqual(App._icon_path(harness), package_icon)
            self.assertEqual(
                App._icon_png_paths(harness),
                [package_png, source_icons / "marketsentinel-256.png"],
            )
            self.assertEqual(App._requirements_path(harness), package_requirements)
            self.assertEqual(App._pyproject_path(harness), package_pyproject)

    def test_gui_lifecycle_smoke_constructs_widget_contract_without_workers(self) -> None:
        expected_tabs = [
            "Markets & Alerts",
            "Paper Trading",
            "Market Safety",
            "Wallet Tracker",
            "Polymarket Analytics",
            "Copy Trading",
            "Logs",
            "About",
        ]

        class FakeNotebook:
            def tabs(self):
                return list(range(len(expected_tabs)))

            def tab(self, tab_id, option):
                self_test.assertEqual(option, "text")
                return expected_tabs[tab_id]

        class FakeApp:
            def __init__(self, *, start_background_workers: bool):
                self.background_workers_argument = start_background_workers
                self._background_workers_started = start_background_workers
                self.notebook = FakeNotebook()
                self.withdrawn = False
                self.updated = False
                self.shutdown_called = False

            def withdraw(self) -> None:
                self.withdrawn = True

            def update_idletasks(self) -> None:
                self.updated = True

            def winfo_exists(self) -> int:
                return 1

            def title(self) -> str:
                return "MarketSentinel"

            def shutdown(self) -> None:
                self.shutdown_called = True

        self_test = self
        created = []

        def app_factory(*, start_background_workers: bool):
            app = FakeApp(start_background_workers=start_background_workers)
            created.append(app)
            return app

        payload = tkinter_gui_lifecycle_smoke_payload(app_factory=app_factory)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["desktop_tabs"], expected_tabs)
        self.assertFalse(payload["background_workers_started"])
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].background_workers_argument)
        self.assertTrue(created[0].withdrawn)
        self.assertTrue(created[0].updated)
        self.assertTrue(created[0].shutdown_called)

    def test_gui_shutdown_stops_workers_cancels_queue_and_is_idempotent(self) -> None:
        class Worker:
            def __init__(self):
                self.stop_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1

        class ShutdownHarness:
            def __init__(self):
                self._shutdown_started = False
                self._leaderboard_cancel_event = threading.Event()
                self.market_ws = Worker()
                self.wallet_poller = Worker()
                self.adapter_price_poller = Worker()
                self._queue_after_id = "queue-callback"
                self.cancelled = []
                self.destroy_calls = 0

            def after_cancel(self, callback_id) -> None:
                self.cancelled.append(callback_id)

            def destroy(self) -> None:
                self.destroy_calls += 1

        harness = ShutdownHarness()

        App.shutdown(harness)
        App.shutdown(harness)

        self.assertTrue(harness._shutdown_started)
        self.assertTrue(harness._leaderboard_cancel_event.is_set())
        self.assertEqual(harness.cancelled, ["queue-callback"])
        self.assertEqual(harness.destroy_calls, 1)
        self.assertEqual(harness.market_ws.stop_calls, 1)
        self.assertEqual(harness.wallet_poller.stop_calls, 1)
        self.assertEqual(harness.adapter_price_poller.stop_calls, 1)

    def test_main_reports_unreadable_configuration_without_starting_gui(self) -> None:
        error = ConfigLoadError("Configuration file cannot be loaded: config.json")

        with (
            patch("app.set_windows_app_id"),
            patch("app.App", side_effect=error),
            patch("app.messagebox.showerror") as show_error,
        ):
            self.assertEqual(main([]), 1)

        show_error.assert_called_once_with("MarketSentinel configuration error", str(error))

    def test_main_smoke_test_emits_machine_readable_payload(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertEqual(main(["--smoke-test"]), 0)

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["tkinter_base"])
        self.assertEqual(payload["app_class"], "App")

    def test_main_gui_smoke_test_reflects_lifecycle_status(self) -> None:
        outcomes = (({"ok": True, "checks": []}, 0), ({"ok": False, "checks": []}, 1))
        for payload, expected_exit_code in outcomes:
            with self.subTest(payload=payload):
                output = io.StringIO()
                with (
                    patch("app.tkinter_gui_lifecycle_smoke_payload", return_value=payload),
                    redirect_stdout(output),
                ):
                    self.assertEqual(main(["--gui-smoke-test"]), expected_exit_code)

                self.assertEqual(json.loads(output.getvalue()), payload)

    def test_main_web_gui_passes_explicit_server_arguments(self) -> None:
        with patch("web_api.run_server") as run_server:
            self.assertEqual(
                main(
                    [
                        "--web-gui",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8766",
                        "--config",
                        "custom-config.json",
                    ]
                ),
                0,
            )

        run_server.assert_called_once_with("127.0.0.1", 8766, Path("custom-config.json"))

    def test_slug_and_float_helpers(self) -> None:
        self.assertEqual(
            extract_slug("https://polymarket.com/event/some-market?tid=123#details"),
            "some-market",
        )
        self.assertEqual(extract_slug("/some-market/"), "some-market")
        self.assertEqual(safe_float("0.25"), 0.25)
        self.assertIsNone(safe_float("bad"))
        self.assertEqual(safe_float("bad", 1.5), 1.5)

    def test_activity_key_prefers_transaction_hash(self) -> None:
        self.assertEqual(activity_key({"transactionHash": "0xABC"}), "tx:0xabc")
        self.assertEqual(
            activity_key({"activityId": "Context:0xabc:0x1"}),
            "activity-id:context:0xabc:0x1",
        )
        fallback = activity_key({"timestamp": 1, "asset": "token", "side": "BUY"})
        self.assertTrue(fallback.startswith("activity:1|"))

    def test_market_choice_label_roundtrip(self) -> None:
        metadata = MarketMetadata(market_id="kalshi", display_name="Kalshi")
        label = market_choice_label(metadata)

        self.assertEqual(label, "Kalshi (kalshi)")
        self.assertEqual(market_id_from_choice(label), "kalshi")
        self.assertEqual(market_id_from_choice("polymarket"), "polymarket")

    def test_market_choices_cover_all_catalog_markets(self) -> None:
        harness = MarketSelectionHarness()

        choices = App._market_choices(harness)
        market_ids = {market_id_from_choice(choice) for choice in choices}

        self.assertEqual(market_ids, set(MARKET_IDS))
        self.assertIn("Polymarket (polymarket)", choices)

    def test_dependency_parser_respects_environment_markers(self) -> None:
        self.assertIsNone(App._parse_requirement_entry('tomli>=2.0.0; python_version < "0"'))

        parsed = App._parse_requirement_entry("websocket-client>=1.7.0")

        self.assertEqual(parsed, {"name": "websocket-client", "display": "websocket-client", "spec": ">=1.7.0"})

    def test_dependency_version_falls_back_to_importable_module_when_metadata_missing(self) -> None:
        class FakeModule:
            __version__ = "1.2.3"

        with patch("app.importlib_metadata.version", side_effect=importlib_metadata.PackageNotFoundError):
            with patch("app.importlib.import_module", return_value=FakeModule()) as import_module:
                installed = App._get_installed_version(object(), "websocket-client")

        self.assertEqual(installed, "1.2.3")
        import_module.assert_called_once_with("websocket")

    def test_dependency_version_marks_importable_versionless_module_installed(self) -> None:
        with patch("app.importlib_metadata.version", side_effect=importlib_metadata.PackageNotFoundError):
            with patch("app.importlib.import_module", return_value=object()):
                installed = App._get_installed_version(object(), "py-clob-client-v2")

        self.assertEqual(installed, "installed")

    def test_desktop_polymarket_analytics_builds_top_roi_query(self) -> None:
        harness = AnalyticsHarness()
        harness.lb_compute_mdd_var.set(True)
        harness.lb_max_mdd_pct_var.set("25")

        params = App._polymarket_leaderboard_params(harness)

        self.assertEqual(params["sort"], ["roi_pct"])
        self.assertEqual(params["direction"], ["DESC"])
        self.assertEqual(params["limit"], ["1000"])
        self.assertEqual(params["scan_limit"], ["1000"])
        self.assertEqual(params["compute_mdd"], ["true"])
        self.assertEqual(params["fast_scan"], ["true"])
        self.assertEqual(params["scan_concurrency"], ["6"])
        self.assertEqual(params["mdd_concurrency"], ["3"])
        self.assertEqual(params["mdd_stop_on_limit"], ["true"])
        self.assertEqual(params["max_mdd_pct"], ["25"])

    def test_desktop_polymarket_analytics_refreshes_table_metrics(self) -> None:
        harness = AnalyticsHarness()

        App._refresh_polymarket_leaderboard_table(
            harness,
            {
                "rows": [
                    {
                        "rank": 1,
                        "display_name": "alpha",
                        "wallet": WALLET,
                        "pnl_usd": 20,
                        "volume_usd": 100,
                        "roi_pct": 20,
                        "trade_count": 4,
                    }
                ],
                "counts": {"returned": 1, "scanned": 1000, "mdd_computed": 0},
                "source": "polymarket_data_api_leaderboard",
                "sort": "roi_pct",
                "direction": "DESC",
            },
        )

        self.assertEqual(harness.lb_returned_metric_var.get(), "1")
        self.assertEqual(harness.lb_scanned_metric_var.get(), "1000")
        self.assertEqual(harness.lb_best_roi_metric_var.get(), "20.00%")
        self.assertEqual(len(harness.leaderboard_tree.rows), 1)
        row_values = next(iter(harness.leaderboard_tree.rows.values()))
        self.assertEqual(row_values[1], "alpha")
        self.assertEqual(row_values[2], WALLET)

    def test_desktop_polymarket_leaderboard_row_actions_copy_full_values(self) -> None:
        harness = AnalyticsHarness()
        App._refresh_polymarket_leaderboard_table(
            harness,
            {
                "rows": [
                    {
                        "rank": 1,
                        "display_name": "alpha-trader",
                        "wallet": WALLET,
                        "roi_pct": 20,
                    }
                ],
                "counts": {"returned": 1, "scanned": 1, "mdd_computed": 0},
            },
        )
        harness.leaderboard_tree.selection_ids = ("leaderboard-0",)

        App.copy_selected_leaderboard_wallet(harness)
        self.assertEqual(harness.clipboard, [WALLET])
        self.assertIn(WALLET, harness.lb_status_var.get())

        App.copy_selected_leaderboard_user(harness)
        self.assertEqual(harness.clipboard, ["alpha-trader"])

    def test_desktop_leaderboard_export_preserves_unknown_risk_and_source_diagnostics(self) -> None:
        harness = AnalyticsHarness()
        quality = {"status": "invalid", "sources": {"closed_positions": {"invalid_rows": 1}}}
        reasons = ["invalid_source_data:closed_positions:invalid_timestamp"]
        harness._last_leaderboard_payload = {"rows": [{
            "wallet": WALLET, "display_name": "Trader", "roi_pct": 10,
            "pnl_volume_pct": 10, "roi_pct_basis": "PnL / volume, not investment ROI",
            "mdd_usd": None, "mdd_pct": None, "mdd_history_status": "invalid_source_data",
            "mdd_source_quality": quality, "mdd_unavailable_reasons": reasons,
            "mdd_account_equity_verified": False,
        }]}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "leaderboard.csv"
            with patch("app.filedialog.asksaveasfilename", return_value=str(target)), patch("app.messagebox.showerror") as error:
                App.export_polymarket_leaderboard(harness)
            error.assert_not_called()
            with target.open(encoding="utf-8", newline="") as stream:
                row, = csv.DictReader(stream)
            self.assertEqual(row["wallet"], WALLET)
            self.assertEqual(row["mdd_pct"], "")
            self.assertEqual(row["mdd_history_status"], "invalid_source_data")
            self.assertEqual(json.loads(row["mdd_source_quality"]), quality)
            self.assertEqual(json.loads(row["mdd_unavailable_reasons"]), reasons)
            self.assertIn("not investment ROI", row["roi_pct_basis"])
            self.assertIn("Exported 1 rows", harness.lb_status_var.get())

    def test_desktop_leaderboard_export_failure_keeps_previous_complete_file(self) -> None:
        harness = AnalyticsHarness()
        harness._last_leaderboard_payload = {"rows": [
            {"wallet": WALLET}, {"wallet": WALLET, "mdd_source_quality": {"invalid": object()}},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "leaderboard.csv"
            target.write_text("previous complete export", encoding="utf-8")
            with patch("app.filedialog.asksaveasfilename", return_value=str(target)), patch("app.messagebox.showerror") as error:
                App.export_polymarket_leaderboard(harness)
            error.assert_called_once()
            self.assertEqual(target.read_text(), "previous complete export")
            self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))
            self.assertEqual(harness.logged, [])

    def test_desktop_polymarket_leaderboard_action_sets_up_copy_trading_follow(self) -> None:
        harness = AnalyticsHarness()
        App._refresh_polymarket_leaderboard_table(
            harness,
            {
                "rows": [
                    {
                        "rank": 1,
                        "display_name": "alpha-trader",
                        "wallet": WALLET,
                        "roi_pct": 20,
                    }
                ],
                "counts": {"returned": 1, "scanned": 1, "mdd_computed": 0},
            },
        )
        harness.leaderboard_tree.selection_ids = ("leaderboard-0",)

        with patch("app.save_config") as save_config:
            App.follow_selected_leaderboard_for_copy_trading(harness)

        self.assertEqual([w.wallet for w in harness.cfg.wallets], [WALLET])
        self.assertEqual(harness.cfg.wallets[0].display_name, "alpha-trader")
        self.assertEqual(harness.cfg.copytrading.normalized_follow_wallets(), [WALLET])
        self.assertEqual(harness.ct_follow_var.get(), WALLET)
        save_config.assert_called_once_with(harness.cfg)

    def test_desktop_polymarket_analytics_cancel_requests_background_stop(self) -> None:
        harness = AnalyticsHarness()
        harness._leaderboard_loading = True
        harness.lb_cancel_btn.state = "normal"

        App.cancel_polymarket_leaderboard_scan(harness)

        self.assertTrue(harness._leaderboard_cancel_event.is_set())
        self.assertEqual(harness.lb_cancel_btn.state, "disabled")
        self.assertIn("Cancelling", harness.lb_status_var.get())
        self.assertEqual(harness.status_var.get(), "Cancelling Polymarket analytics scan...")
        self.assertIn("[analytics] cancel requested", harness.logged)

    def test_market_change_persists_kalshi_selection_without_gui_window(self) -> None:
        harness = MarketSelectionHarness()
        harness.market_var.set("Kalshi (kalshi)")

        with patch("app.save_config") as save_config:
            App._on_market_change(harness)

        self.assertEqual(harness.cfg.selected_market_id, "kalshi")
        self.assertEqual(harness.market_var.get(), "Kalshi (kalshi)")
        self.assertEqual(harness.status_var.get(), "Selected market: Kalshi.")
        self.assertIn("Kalshi: adapter loaded.", harness.market_status_var.get())
        self.assertIn("live guarded/off", harness.market_status_var.get())
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        save_config.assert_called_once_with(harness.cfg)

    def test_robinhood_distribution_alias_status_is_loaded_and_guarded(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.selected_market_id = "robinhood_prediction_markets"

        status = App._selected_market_status_text(harness)

        self.assertIn("Robinhood Prediction Markets: adapter loaded.", status)
        self.assertIn("read-only yes", status)
        self.assertIn("paper yes", status)
        self.assertIn("live no", status)

    def test_market_safety_refresh_populates_selected_market_settings(self) -> None:
        harness = SafetyHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.cfg.markets["kalshi"].settings.update(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": False,
                "live_trading_kill_switch": True,
                "live_trading_max_notional": 12.5,
                "credential_env_vars": ["KALSHI_API_KEY_ID"],
            }
        )

        App._refresh_market_safety_tab(harness)

        self.assertEqual(harness.safety_market_var.get(), "Selected market: Kalshi (kalshi)")
        self.assertTrue(harness.safety_market_enabled_var.get())
        self.assertTrue(harness.safety_live_enabled_var.get())
        self.assertFalse(harness.safety_live_confirmed_var.get())
        self.assertTrue(harness.safety_kill_switch_var.get())
        self.assertEqual(harness.safety_max_notional_var.get(), "12.5")
        self.assertIn("kill-switch", harness.safety_status_var.get())
        self.assertEqual(harness.safety_tree.rows["credential_env_vars"][1], "KALSHI_API_KEY_ID")
        self.assertIn("event_listing", harness.safety_tree.rows["capabilities"][1])

    def test_save_market_safety_settings_persists_live_gates(self) -> None:
        harness = SafetyHarness()
        harness.safety_market_enabled_var.set(True)
        harness.safety_live_enabled_var.set(True)
        harness.safety_live_confirmed_var.set(True)
        harness.safety_kill_switch_var.set(False)
        harness.safety_max_size_var.set("9")
        harness.safety_max_notional_var.set("25.5")

        with patch("app.save_config") as save_config:
            App.save_market_safety_settings(harness)

        settings = harness.cfg.markets["kalshi"].settings
        self.assertTrue(harness.cfg.markets["kalshi"].enabled)
        self.assertTrue(settings["live_trading_enabled"])
        self.assertTrue(settings["live_trading_confirmed"])
        self.assertFalse(settings["live_trading_kill_switch"])
        self.assertEqual(settings["live_trading_max_size"], 9.0)
        self.assertEqual(settings["live_trading_max_notional"], 25.5)
        self.assertIn("live armed", harness.market_status_var.get())
        self.assertEqual(harness.status_var.get(), "Market safety settings saved.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        save_config.assert_called_once_with(harness.cfg)

    def test_save_market_safety_settings_rejects_invalid_caps(self) -> None:
        harness = SafetyHarness()
        harness.safety_max_notional_var.set("bad")

        with patch("app.save_config") as save_config, patch("app.messagebox.showerror") as showerror:
            App.save_market_safety_settings(harness)

        self.assertIn("Max notional", harness.safety_status_var.get())
        save_config.assert_not_called()
        showerror.assert_called_once()

    def test_non_polymarket_selection_blocks_wallet_only_actions(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.selected_market_id = "kalshi"

        with patch("app.messagebox.showinfo") as showinfo:
            result = App._require_polymarket_selected(harness, "Wallet tracking")

        self.assertFalse(result)
        self.assertIn("currently implemented only for Polymarket", harness.status_var.get())
        self.assertIn("has not been generalized", harness.status_var.get())
        self.assertIn("Selected adapter status: Kalshi: adapter loaded.", harness.status_var.get())
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        showinfo.assert_called_once()

    def test_polymarket_selection_allows_wallet_only_actions(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.selected_market_id = "polymarket"

        with patch("app.messagebox.showinfo") as showinfo:
            result = App._require_polymarket_selected(harness, "Wallet tracking")

        self.assertTrue(result)
        self.assertTrue(harness.ui_queue.empty())
        showinfo.assert_not_called()

    def test_adapter_event_loaded_populates_generic_contract_selection(self) -> None:
        harness = MarketSelectionHarness()
        harness.market_info_var = FakeVar()
        harness.outcome_list = FakeListbox()
        harness.alert_label_entry = FakeEntry()
        harness._market_outcomes = []
        harness._selected_token_id = None
        harness._selected_alert_market_id = "polymarket"
        adapter = harness.adapter_registry.create("kalshi")
        event = MarketEvent("kalshi", "EVT", "Fed decision", status="open")
        contract = MarketContract("kalshi", "FED-YES:yes", "EVT", "Fed decision - Yes", outcome="Yes")

        App._set_adapter_event_loaded(harness, adapter, event, [contract])

        self.assertEqual(harness._market_outcomes, [contract])
        self.assertEqual(harness._selected_alert_market_id, "kalshi")
        self.assertIn("Fed decision", harness.market_info_var.get())
        self.assertIn("contract FED-YES:yes", harness.outcome_list.items[0])

        harness.outcome_list.selection = (0,)
        App._on_outcome_selected(harness)

        self.assertEqual(harness._selected_token_id, "FED-YES:yes")
        self.assertEqual(harness._selected_alert_market_id, "kalshi")
        self.assertEqual(harness.alert_label_entry.get(), "Yes")

    def test_once_alert_fires_and_disables_on_crossing(self) -> None:
        alert = PriceAlert(
            token_id="token-1",
            label="Yes",
            direction="above",
            threshold=0.5,
            source="last_trade",
            once=True,
        )
        harness = AlertHarness(alert, 0.51)

        with patch("app.save_config") as save_config:
            App._eval_alerts_for_token(harness, "token-1")

        self.assertEqual(harness.fired, [(alert.id, 0.51)])
        self.assertTrue(alert.triggered)
        self.assertFalse(alert.enabled)
        self.assertTrue(harness.refreshed)
        save_config.assert_called_once()

    def test_market_scoped_alerts_do_not_collide_on_same_contract_id(self) -> None:
        polymarket_alert = PriceAlert(
            token_id="same-contract",
            label="Poly",
            direction="above",
            threshold=0.5,
            source="last_trade",
            market_id="polymarket",
        )
        kalshi_alert = PriceAlert(
            token_id="same-contract",
            label="Kalshi",
            direction="above",
            threshold=0.5,
            source="last_trade",
            market_id="kalshi",
        )
        harness = MultiAlertHarness(
            [polymarket_alert, kalshi_alert],
            {
                App._price_state_key("polymarket", "same-contract"): {"last_trade": 0.40},
                App._price_state_key("kalshi", "same-contract"): {"last_trade": 0.70},
            },
        )

        with patch("app.save_config") as save_config:
            App._eval_alerts_for_contract(harness, "kalshi", "same-contract")

        self.assertEqual(harness.fired, [("kalshi", "same-contract", 0.70)])
        self.assertFalse(polymarket_alert.triggered)
        self.assertTrue(kalshi_alert.triggered)
        save_config.assert_called_once()

    def test_adapter_price_poller_emits_updates_for_websocket_and_non_websocket_markets(self) -> None:
        adapter = FakePriceAdapter()
        registry = FakeRegistry(adapter)
        ui_queue: "queue.Queue[tuple]" = queue.Queue()
        cfg = AppConfig(
            alerts=[
                PriceAlert(
                    token_id="KALSHI-CONTRACT",
                    label="Kalshi",
                    direction="above",
                    threshold=0.6,
                    market_id="kalshi",
                ),
                PriceAlert(
                    token_id="POLY-TOKEN",
                    label="Poly",
                    direction="above",
                    threshold=0.6,
                    market_id="polymarket",
                ),
            ]
        )
        cfg.markets["kalshi"].enabled = True
        poller = AdapterPricePoller(ui_queue, cfg, registry)

        poller.poll_once()

        updates = [ui_queue.get_nowait(), ui_queue.get_nowait()]
        self.assertTrue(all(kind == "adapter_price" for kind, _payload, _unused in updates))
        payloads = {payload["market_id"]: payload for _kind, payload, _unused in updates}
        self.assertEqual(set(payloads), {"kalshi", "polymarket"})
        self.assertEqual(payloads["kalshi"]["contract_id"], "KALSHI-CONTRACT")
        self.assertEqual(payloads["polymarket"]["contract_id"], "POLY-TOKEN")
        self.assertEqual(payloads["kalshi"]["values"]["last_trade"], 0.62)
        self.assertEqual(payloads["polymarket"]["values"]["midpoint"], 0.62)
        self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT", "POLY-TOKEN"])
        self.assertEqual([call[0] for call in registry.calls], ["kalshi", "polymarket"])

    def test_adapter_price_poller_skips_market_with_connected_stream(self) -> None:
        adapter = FakePriceAdapter()
        registry = FakeRegistry(adapter)
        ui_queue: "queue.Queue[tuple]" = queue.Queue()
        cfg = AppConfig(
            alerts=[
                PriceAlert(
                    token_id="KALSHI-CONTRACT",
                    label="Kalshi",
                    direction="above",
                    threshold=0.6,
                    market_id="kalshi",
                ),
                PriceAlert(
                    token_id="POLY-TOKEN",
                    label="Poly",
                    direction="above",
                    threshold=0.6,
                    market_id="polymarket",
                ),
            ]
        )
        cfg.markets["kalshi"].enabled = True
        poller = AdapterPricePoller(
            ui_queue,
            cfg,
            registry,
            stream_connected=lambda market_id: market_id == "polymarket",
        )

        poller.poll_once()

        kind, payload, _unused = ui_queue.get_nowait()
        self.assertEqual(kind, "adapter_price")
        self.assertEqual(payload["market_id"], "kalshi")
        self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT"])
        self.assertEqual([call[0] for call in registry.calls], ["kalshi"])
        self.assertTrue(ui_queue.empty())

    def test_adapter_price_poller_skips_disabled_markets(self) -> None:
        adapter = FakePriceAdapter()
        registry = FakeRegistry(adapter)
        ui_queue: "queue.Queue[tuple]" = queue.Queue()
        cfg = AppConfig(
            alerts=[
                PriceAlert(
                    token_id="KALSHI-CONTRACT",
                    label="Kalshi",
                    direction="above",
                    threshold=0.6,
                    market_id="kalshi",
                )
            ]
        )
        poller = AdapterPricePoller(ui_queue, cfg, registry)

        poller.poll_once()

        self.assertEqual(adapter.contracts, [])
        self.assertEqual(registry.calls, [])
        kind, message = ui_queue.get_nowait()
        self.assertEqual(kind, "log")
        self.assertIn("disabled in local market config", message)

    def test_paper_order_form_builds_adapter_request_with_optional_limit(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("7")
        harness.paper_limit_var = FakeVar("")

        order = App._paper_order_from_form(harness)

        self.assertEqual(order.market_id, "kalshi")
        self.assertEqual(order.contract_id, "KALSHI-CONTRACT")
        self.assertEqual(order.side, "BUY")
        self.assertEqual(order.size, 7.0)
        self.assertIsNone(order.limit_price)

    def test_submit_paper_order_records_adapter_result_in_history(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("4")
        harness.paper_limit_var = FakeVar("0.44")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.paper_tree = FakeTree()
        harness.ui_queue = queue.Queue()

        with patch("app.save_config") as save_config:
            App.submit_paper_order(harness)

        self.assertEqual(len(adapter.orders), 1)
        self.assertEqual(adapter.orders[0].limit_price, 0.44)
        self.assertEqual(len(harness.cfg.paper_trades), 1)
        record = harness.cfg.paper_trades[0]
        self.assertEqual(record.market_id, "kalshi")
        self.assertEqual(record.contract_id, "KALSHI-CONTRACT")
        self.assertEqual(record.message, "DRY RUN accepted")
        self.assertEqual(harness.paper_status_var.get(), "DRY RUN accepted")
        self.assertEqual(harness.status_var.get(), "Paper order recorded.")
        self.assertEqual(len(harness.paper_tree.rows), 1)
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        save_config.assert_called_once_with(harness.cfg)

    def test_paper_position_rows_aggregate_accepted_history_by_contract(self) -> None:
        rows = App._paper_position_rows(
            [
                PaperTradeRecord(
                    market_id="kalshi",
                    contract_id="KALSHI-CONTRACT",
                    side="BUY",
                    size=4,
                    limit_price=0.40,
                    accepted=True,
                    message="buy",
                    filled_size=4,
                    average_price=0.40,
                ),
                PaperTradeRecord(
                    market_id="kalshi",
                    contract_id="KALSHI-CONTRACT",
                    side="SELL",
                    size=1,
                    limit_price=0.55,
                    accepted=True,
                    message="sell",
                    filled_size=1,
                    average_price=0.55,
                ),
                PaperTradeRecord(
                    market_id="kalshi",
                    contract_id="IGNORED",
                    side="BUY",
                    size=8,
                    limit_price=0.70,
                    accepted=False,
                    message="rejected",
                ),
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_id"], "kalshi")
        self.assertEqual(rows[0]["contract_id"], "KALSHI-CONTRACT")
        self.assertAlmostEqual(rows[0]["net_size"], 3.0)
        self.assertAlmostEqual(rows[0]["notional"], 1.05)
        self.assertAlmostEqual(rows[0]["average_price"], 0.35)
        self.assertEqual(rows[0]["trades"], 2)

    def test_paper_order_impact_projects_position_after_order(self) -> None:
        impact = App._paper_order_impact(
            [
                PaperTradeRecord(
                    market_id="kalshi",
                    contract_id="KALSHI-CONTRACT",
                    side="BUY",
                    size=4,
                    limit_price=0.40,
                    accepted=True,
                    message="buy",
                )
            ],
            PaperOrderRequest(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="SELL",
                size=1,
                limit_price=0.55,
            ),
        )

        self.assertAlmostEqual(impact["current_net"], 4.0)
        self.assertAlmostEqual(impact["signed_size"], -1.0)
        self.assertAlmostEqual(impact["projected_net"], 3.0)
        self.assertEqual(impact["effect"], "reduces position")
        self.assertAlmostEqual(impact["order_notional"], -0.55)
        self.assertAlmostEqual(impact["projected_notional"], 1.05)
        self.assertAlmostEqual(impact["projected_average"], 0.35)

    def test_preview_paper_order_impact_reports_projection_without_ordering(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=4,
                limit_price=0.40,
                accepted=True,
                message="buy",
            )
        ]
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("SELL")
        harness.paper_size_var = FakeVar("1")
        harness.paper_limit_var = FakeVar("0.55")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.preview_paper_order_impact(harness)

        self.assertEqual(adapter.orders, [])
        self.assertEqual(harness.adapter_registry.calls, [])
        self.assertIn("projected_net=3.0000", harness.paper_status_var.get())
        self.assertIn("effect=reduces position", harness.paper_status_var.get())
        self.assertIn("projected_avg=0.3500", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Paper order impact previewed.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_refresh_paper_trade_table_updates_position_summary(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_tree = FakeTree()
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="buy",
            )
        ]

        App._refresh_paper_trade_table(harness)

        self.assertEqual(len(harness.paper_tree.rows), 1)
        self.assertEqual(len(harness.paper_position_tree.rows), 1)
        row = next(iter(harness.paper_position_tree.rows.values()))
        self.assertEqual(row[0], "kalshi")
        self.assertEqual(row[1], "KALSHI-CONTRACT")
        self.assertEqual(row[2], "2.0000")
        self.assertEqual(row[3], "0.4400")
        self.assertEqual(row[4], "0.8800")
        self.assertIn("Positions: 1", harness.paper_position_summary_var.get())
        self.assertIn("gross_size=2.0000", harness.paper_position_summary_var.get())
        self.assertIn("entry_notional=0.8800", harness.paper_position_summary_var.get())

    def test_format_paper_position_summary_includes_marked_unrealized_total(self) -> None:
        rows = App._paper_position_rows(
            [
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
        )
        marked_at = 1_712_345_678
        marks = {
            ("kalshi", "KALSHI-CONTRACT"): {
                "mark_price": 0.60,
                "unrealized": 0.32,
                "source": "bid",
                "marked_at": marked_at,
            }
        }

        summary = App._format_paper_position_summary(rows, marks)

        self.assertIn("Positions: 1", summary)
        self.assertIn("marked=1/1", summary)
        self.assertIn("unrealized=0.3200", summary)
        self.assertIn(f"last_mark={time.strftime('%H:%M:%S', time.localtime(marked_at))}", summary)
        self.assertIn("mark_sources=bid:1", summary)

    def test_refresh_paper_position_table_revalues_cached_mark_against_current_history(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness._paper_position_marks = {
            ("kalshi", "KALSHI-CONTRACT"): {
                "mark_price": 0.60,
                "unrealized": 0.32,
                "source": "bid",
                "marked_at": 1_712_345_678,
            },
            ("kalshi", "CLOSED-CONTRACT"): {
                "mark_price": 0.10,
                "source": "last",
                "marked_at": 1_712_345_600,
            },
        }
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="first",
            ),
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=1,
                limit_price=0.50,
                accepted=True,
                message="second",
            ),
        ]

        App._refresh_paper_position_table(harness)

        row = next(iter(harness.paper_position_tree.rows.values()))
        self.assertEqual(row[2], "3.0000")
        self.assertEqual(row[8], "0.4200")
        self.assertEqual(row[9], 2)
        self.assertEqual(set(harness._paper_position_marks), {("kalshi", "KALSHI-CONTRACT")})
        self.assertIn("unrealized=0.4200", harness.paper_position_summary_var.get())

    def test_paper_marks_for_rows_drops_non_active_and_malformed_keys(self) -> None:
        rows = [{"market_id": "kalshi", "contract_id": "KALSHI-CONTRACT"}]
        marks = {
            ("kalshi", "KALSHI-CONTRACT"): {"mark_price": 0.60},
            ("kalshi", "CLOSED-CONTRACT"): {"mark_price": 0.10},
            "bad-key": {"mark_price": 0.99},
        }

        pruned = App._paper_marks_for_rows(marks, rows)

        self.assertEqual(pruned, {("kalshi", "KALSHI-CONTRACT"): {"mark_price": 0.60}})

    def test_use_selected_paper_trade_loads_history_into_form(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        record = PaperTradeRecord(
            market_id="kalshi",
            contract_id="KALSHI-CONTRACT",
            side="BUY",
            size=4,
            limit_price=0.44,
            accepted=True,
            message="accepted",
        )
        harness.cfg.paper_trades = [record]
        harness.paper_tree = FakeTree()
        harness.paper_position_tree = FakeTree()
        harness.paper_market_var = FakeVar("")
        harness.paper_contract_var = FakeVar("")
        harness.paper_side_var = FakeVar("SELL")
        harness.paper_size_var = FakeVar("")
        harness.paper_limit_var = FakeVar("")
        harness.paper_selected_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.paper_submit_btn = FakeButton()
        harness.paper_quote_btn = FakeButton()
        harness.paper_quote_limit_btn = FakeButton()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_trade_table(harness)
        harness.paper_tree.selection_ids = (record.id,)
        App.use_selected_paper_trade(harness)

        self.assertEqual(harness.paper_market_var.get(), "kalshi")
        self.assertEqual(harness.paper_contract_var.get(), "KALSHI-CONTRACT")
        self.assertEqual(harness.paper_side_var.get(), "BUY")
        self.assertEqual(harness.paper_size_var.get(), "4")
        self.assertEqual(harness.paper_limit_var.get(), "0.44")
        self.assertEqual(harness.paper_selected_var.get(), "Selected contract: kalshi:KALSHI-CONTRACT")
        self.assertEqual(harness.paper_submit_btn.state, "normal")
        self.assertEqual(harness.status_var.get(), "Paper order loaded into form.")
        self.assertIn("Loaded paper history order", harness.paper_status_var.get())
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        self.assertEqual(adapter.orders, [])

    def test_use_selected_paper_trade_requires_selection(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_tree = FakeTree()
        harness.paper_status_var = FakeVar()

        with patch("app.messagebox.showerror") as showerror:
            App.use_selected_paper_trade(harness)

        self.assertIn("Select a paper order history row first", harness.paper_status_var.get())
        showerror.assert_called_once()

    def test_use_selected_paper_position_loads_close_sized_order(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        record = PaperTradeRecord(
            market_id="kalshi",
            contract_id="KALSHI-CONTRACT",
            side="BUY",
            size=4,
            limit_price=0.44,
            accepted=True,
            message="accepted",
        )
        harness.cfg.paper_trades = [record]
        harness.paper_tree = FakeTree()
        harness.paper_position_tree = FakeTree()
        harness.paper_market_var = FakeVar("")
        harness.paper_contract_var = FakeVar("")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("")
        harness.paper_limit_var = FakeVar("0.99")
        harness.paper_selected_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.paper_submit_btn = FakeButton()
        harness.paper_quote_btn = FakeButton()
        harness.paper_quote_limit_btn = FakeButton()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_trade_table(harness)
        harness.paper_position_tree.selection_ids = ("kalshi:KALSHI-CONTRACT",)
        App.use_selected_paper_position(harness)

        self.assertEqual(harness.paper_market_var.get(), "kalshi")
        self.assertEqual(harness.paper_contract_var.get(), "KALSHI-CONTRACT")
        self.assertEqual(harness.paper_side_var.get(), "SELL")
        self.assertEqual(harness.paper_size_var.get(), "4")
        self.assertEqual(harness.paper_limit_var.get(), "")
        self.assertEqual(harness.paper_selected_var.get(), "Selected contract: kalshi:KALSHI-CONTRACT")
        self.assertEqual(harness.paper_submit_btn.state, "normal")
        self.assertEqual(harness.status_var.get(), "Paper position loaded into form.")
        self.assertIn("Loaded paper position into form", harness.paper_status_var.get())
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        self.assertEqual(adapter.orders, [])

    def test_use_selected_paper_position_uses_back_lay_side_family(self) -> None:
        side = App._paper_position_close_side(
            [
                PaperTradeRecord(
                    market_id="azuro",
                    contract_id="GAME:HOME",
                    side="BACK",
                    size=2,
                    limit_price=0.50,
                    accepted=True,
                    message="accepted",
                )
            ],
            "azuro",
            "GAME:HOME",
            2.0,
        )

        self.assertEqual(side, "LAY")

    def test_use_selected_paper_position_requires_selection(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_position_tree = FakeTree()
        harness.paper_status_var = FakeVar()

        with patch("app.messagebox.showerror") as showerror:
            App.use_selected_paper_position(harness)

        self.assertIn("Select a paper exposure row first", harness.paper_status_var.get())
        showerror.assert_called_once()

    def test_refresh_paper_position_marks_updates_mark_and_unrealized_pnl(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.cfg.paper_trades = [
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
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        marked_at = 1_712_345_678
        with patch("app.time.time", return_value=marked_at):
            App.refresh_paper_position_marks(harness)

        self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT"])
        row = next(iter(harness.paper_position_tree.rows.values()))
        self.assertEqual(row[5], "0.6000")
        self.assertEqual(row[6], "bid")
        self.assertEqual(row[7], time.strftime("%H:%M:%S", time.localtime(marked_at)))
        self.assertEqual(row[8], "0.3200")
        self.assertEqual(row[9], 1)
        self.assertIn("marked=1/1", harness.paper_position_summary_var.get())
        self.assertIn("unrealized=0.3200", harness.paper_position_summary_var.get())
        self.assertIn(f"last_mark={time.strftime('%H:%M:%S', time.localtime(marked_at))}", harness.paper_position_summary_var.get())
        self.assertIn("mark_sources=bid:1", harness.paper_position_summary_var.get())
        self.assertIn("Marked 1/1", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Paper exposure marks refreshed.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_paper_position_mark_price_prefers_close_side_quote(self) -> None:
        snapshot = PriceSnapshot(
            market_id="kalshi",
            contract_id="KALSHI-CONTRACT",
            last=0.62,
            bid=0.60,
            ask=0.64,
            source="unit-test",
        )

        self.assertEqual(App._paper_position_mark_price(snapshot, 2.0), (0.60, "bid"))
        self.assertEqual(App._paper_position_mark_price(snapshot, -2.0), (0.64, "ask"))

    def test_refresh_paper_position_marks_skips_disabled_market_without_adapter_call(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.adapter_registry = FakeRegistry(adapter)
        harness.cfg.paper_trades = [
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
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.refresh_paper_position_marks(harness)

        self.assertEqual(harness.adapter_registry.calls, [])
        row = next(iter(harness.paper_position_tree.rows.values()))
        self.assertEqual(row[5], "")
        self.assertEqual(row[6], "")
        self.assertEqual(row[7], "")
        self.assertEqual(row[8], "")
        self.assertIn("Marked 0/1", harness.paper_status_var.get())
        self.assertIn("disabled", harness.paper_status_var.get())

    def test_refresh_selected_paper_position_mark_updates_only_selected_mark(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="selected",
            ),
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="OTHER-CONTRACT",
                side="BUY",
                size=1,
                limit_price=0.33,
                accepted=True,
                message="other",
            ),
        ]
        harness._paper_position_marks = {
            ("kalshi", "OTHER-CONTRACT"): {
                "mark_price": 0.50,
                "source": "last",
                "marked_at": 1_700_000_000,
            }
        }
        harness.paper_tree = FakeTree()
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_trade_table(harness)
        harness.paper_position_tree.selection_ids = ("kalshi:KALSHI-CONTRACT",)
        marked_at = 1_712_345_678
        with patch("app.time.time", return_value=marked_at):
            App.refresh_selected_paper_position_mark(harness)

        self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT"])
        self.assertEqual(set(harness._paper_position_marks), {("kalshi", "KALSHI-CONTRACT"), ("kalshi", "OTHER-CONTRACT")})
        selected_mark = harness._paper_position_marks[("kalshi", "KALSHI-CONTRACT")]
        self.assertEqual(selected_mark["mark_price"], 0.60)
        self.assertEqual(selected_mark["source"], "bid")
        self.assertEqual(selected_mark["marked_at"], marked_at)
        self.assertEqual(harness._paper_position_marks[("kalshi", "OTHER-CONTRACT")]["mark_price"], 0.50)
        row = harness.paper_position_tree.rows["kalshi:KALSHI-CONTRACT"]
        self.assertEqual(row[5], "0.6000")
        self.assertEqual(row[6], "bid")
        self.assertEqual(row[7], time.strftime("%H:%M:%S", time.localtime(marked_at)))
        self.assertIn("Marked selected paper position", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Selected paper exposure mark refreshed.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_refresh_selected_paper_position_mark_requires_selection(self) -> None:
        harness = MarketSelectionHarness()
        harness.paper_position_tree = FakeTree()
        harness.paper_status_var = FakeVar()

        with patch("app.messagebox.showerror") as showerror:
            App.refresh_selected_paper_position_mark(harness)

        self.assertEqual(harness.paper_status_var.get(), "Selected paper mark refresh failed.")
        showerror.assert_called_once()

    def test_clear_selected_paper_position_mark_preserves_other_marks(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="selected",
            ),
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="OTHER-CONTRACT",
                side="BUY",
                size=1,
                limit_price=0.33,
                accepted=True,
                message="other",
            ),
        ]
        harness._paper_position_marks = {
            ("kalshi", "KALSHI-CONTRACT"): {
                "mark_price": 0.60,
                "source": "bid",
                "marked_at": 1_712_345_678,
            },
            ("kalshi", "OTHER-CONTRACT"): {
                "mark_price": 0.50,
                "source": "last",
                "marked_at": 1_700_000_000,
            },
        }
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_position_table(harness)
        harness.paper_position_tree.selection_ids = ("kalshi:KALSHI-CONTRACT",)
        App.clear_selected_paper_position_mark(harness)

        self.assertEqual(set(harness._paper_position_marks), {("kalshi", "OTHER-CONTRACT")})
        selected_row = harness.paper_position_tree.rows["kalshi:KALSHI-CONTRACT"]
        self.assertEqual(selected_row[5], "")
        self.assertEqual(selected_row[6], "")
        self.assertEqual(selected_row[7], "")
        self.assertEqual(selected_row[8], "")
        other_row = harness.paper_position_tree.rows["kalshi:OTHER-CONTRACT"]
        self.assertEqual(other_row[5], "0.5000")
        self.assertEqual(other_row[6], "last")
        self.assertEqual(len(harness.cfg.paper_trades), 2)
        self.assertIn("marked=1/2", harness.paper_position_summary_var.get())
        self.assertEqual(harness.paper_status_var.get(), "Cleared selected paper exposure mark: kalshi:KALSHI-CONTRACT")
        self.assertEqual(harness.status_var.get(), "Selected paper exposure mark cleared.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_clear_selected_paper_position_mark_reports_missing_mark(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.paper_trades = [
            PaperTradeRecord(
                market_id="kalshi",
                contract_id="KALSHI-CONTRACT",
                side="BUY",
                size=2,
                limit_price=0.44,
                accepted=True,
                message="selected",
            )
        ]
        harness._paper_position_marks = {}
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_position_table(harness)
        harness.paper_position_tree.selection_ids = ("kalshi:KALSHI-CONTRACT",)
        App.clear_selected_paper_position_mark(harness)

        self.assertEqual(harness._paper_position_marks, {})
        self.assertIn("No paper exposure mark to clear", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "No selected paper exposure mark to clear.")
        self.assertTrue(harness.ui_queue.empty())

    def test_clear_paper_position_marks_removes_marks_without_clearing_history(self) -> None:
        harness = MarketSelectionHarness()
        harness.cfg.paper_trades = [
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
        marked_at = 1_712_345_678
        harness._paper_position_marks = {
            ("kalshi", "KALSHI-CONTRACT"): {
                "mark_price": 0.60,
                "source": "bid",
                "marked_at": marked_at,
            }
        }
        harness.paper_position_tree = FakeTree()
        harness.paper_position_summary_var = FakeVar()
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App._refresh_paper_position_table(harness)
        self.assertIn("unrealized=", harness.paper_position_summary_var.get())

        App.clear_paper_position_marks(harness)

        self.assertEqual(harness._paper_position_marks, {})
        self.assertEqual(len(harness.cfg.paper_trades), 1)
        row = harness.paper_position_tree.rows["kalshi:KALSHI-CONTRACT"]
        self.assertEqual(row[5], "")
        self.assertEqual(row[6], "")
        self.assertEqual(row[7], "")
        self.assertEqual(row[8], "")
        self.assertEqual(row[9], 1)
        summary = harness.paper_position_summary_var.get()
        self.assertIn("marked=0/1", summary)
        self.assertNotIn("unrealized=", summary)
        self.assertNotIn("last_mark=", summary)
        self.assertEqual(harness.paper_status_var.get(), "Paper exposure marks cleared.")
        self.assertEqual(harness.status_var.get(), "Paper exposure marks cleared.")
        self.assertEqual(harness.ui_queue.get_nowait(), ("log", "[paper] Paper exposure marks cleared."))

    def test_clear_paper_position_marks_reports_when_empty(self) -> None:
        harness = MarketSelectionHarness()
        harness._paper_position_marks = {}
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.clear_paper_position_marks(harness)

        self.assertEqual(harness.paper_status_var.get(), "No paper exposure marks to clear.")
        self.assertEqual(harness.status_var.get(), "No paper exposure marks to clear.")
        self.assertTrue(harness.ui_queue.empty())

    def test_refresh_paper_quote_reads_price_and_orderbook_without_ordering(self) -> None:
        adapter = FakeQuoteAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.refresh_paper_quote(harness)

        self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT"])
        self.assertEqual(adapter.orderbooks, ["KALSHI-CONTRACT"])
        self.assertEqual(adapter.orders, [])
        self.assertIn("Quote: Kalshi", harness.paper_status_var.get())
        self.assertIn("last=0.62", harness.paper_status_var.get())
        self.assertIn("best_bid=0.58x12", harness.paper_status_var.get())
        self.assertIn("best_ask=0.66x15", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Paper quote refreshed.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_use_quote_limit_for_paper_sets_side_aware_limit_without_ordering(self) -> None:
        cases = (("BUY", "0.66", "best_ask"), ("SELL", "0.58", "best_bid"))
        for side, expected_limit, expected_source in cases:
            with self.subTest(side=side):
                adapter = FakeQuoteAdapter()
                harness = MarketSelectionHarness()
                harness.cfg.markets["kalshi"].enabled = True
                harness.adapter_registry = FakeRegistry(adapter)
                harness.paper_market_var = FakeVar("kalshi")
                harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
                harness.paper_side_var = FakeVar(side)
                harness.paper_limit_var = FakeVar("")
                harness.paper_status_var = FakeVar()
                harness.status_var = FakeVar()
                harness.ui_queue = queue.Queue()

                App.use_quote_limit_for_paper(harness)

                self.assertEqual(harness.paper_limit_var.get(), expected_limit)
                self.assertEqual(adapter.contracts, ["KALSHI-CONTRACT"])
                self.assertEqual(adapter.orderbooks, ["KALSHI-CONTRACT"])
                self.assertEqual(adapter.orders, [])
                self.assertIn(expected_source, harness.paper_status_var.get())
                self.assertEqual(harness.status_var.get(), "Paper limit updated from quote.")
                self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_live_preflight_preview_uses_paper_order_form_without_submitting(self) -> None:
        adapter = FakeLiveAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("4")
        harness.paper_limit_var = FakeVar("0.44")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.preview_live_preflight(harness)

        self.assertEqual(len(adapter.preflight_orders), 1)
        order, feature_name = adapter.preflight_orders[0]
        self.assertEqual(order.contract_id, "KALSHI-CONTRACT")
        self.assertEqual(feature_name, "live preflight preview")
        self.assertEqual(adapter.orders, [])
        self.assertIn("Preflight OK", harness.paper_status_var.get())
        self.assertIn("notional~1.76", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Live preflight preview passed.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_live_preflight_preview_reports_blocked_gate(self) -> None:
        adapter = FakeLiveAdapter(error=MarketConfigurationError("needs acknowledgement"))
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("4")
        harness.paper_limit_var = FakeVar("0.44")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        App.preview_live_preflight(harness)

        self.assertIn("Live preflight blocked", harness.paper_status_var.get())
        self.assertIn("needs acknowledgement", harness.paper_status_var.get())
        self.assertEqual(harness.status_var.get(), "Live preflight blocked.")
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")

    def test_paper_market_state_disables_submit_for_unsupported_adapter(self) -> None:
        adapter = FakePriceAdapter()
        harness = MarketSelectionHarness()
        harness.cfg.markets["kalshi"].enabled = True
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_status_var = FakeVar()
        harness.paper_submit_btn = FakeButton()
        harness.paper_quote_btn = FakeButton()
        harness.paper_quote_limit_btn = FakeButton()

        App._refresh_paper_market_state(harness)

        self.assertEqual(harness.paper_submit_btn.state, "disabled")
        self.assertEqual(harness.paper_quote_btn.state, "normal")
        self.assertEqual(harness.paper_quote_limit_btn.state, "normal")
        self.assertIn("does not support paper trading", harness.paper_status_var.get())

    def test_paper_market_state_disables_all_actions_for_disabled_market(self) -> None:
        adapter = FakeQuoteAdapter()
        harness = MarketSelectionHarness()
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_status_var = FakeVar()
        harness.paper_submit_btn = FakeButton()
        harness.paper_quote_btn = FakeButton()
        harness.paper_quote_limit_btn = FakeButton()

        App._refresh_paper_market_state(harness)

        self.assertEqual(harness.paper_submit_btn.state, "disabled")
        self.assertEqual(harness.paper_quote_btn.state, "disabled")
        self.assertEqual(harness.paper_quote_limit_btn.state, "disabled")
        self.assertEqual(harness.adapter_registry.calls, [])
        self.assertIn("disabled in local market config", harness.paper_status_var.get())

    def test_submit_paper_order_blocks_disabled_market_without_ordering(self) -> None:
        adapter = FakePaperAdapter()
        harness = MarketSelectionHarness()
        harness.adapter_registry = FakeRegistry(adapter)
        harness.paper_market_var = FakeVar("kalshi")
        harness.paper_contract_var = FakeVar("KALSHI-CONTRACT")
        harness.paper_side_var = FakeVar("BUY")
        harness.paper_size_var = FakeVar("4")
        harness.paper_limit_var = FakeVar("0.44")
        harness.paper_status_var = FakeVar()
        harness.status_var = FakeVar()
        harness.ui_queue = queue.Queue()

        with patch("app.messagebox.showinfo") as showinfo:
            App.submit_paper_order(harness)

        self.assertEqual(adapter.orders, [])
        self.assertEqual(harness.adapter_registry.calls, [])
        self.assertIn("disabled in local market config", harness.paper_status_var.get())
        self.assertEqual(harness.ui_queue.get_nowait()[0], "log")
        showinfo.assert_called_once()

    def test_repeat_alert_resets_after_condition_clears(self) -> None:
        alert = PriceAlert(
            token_id="token-1",
            label="Yes",
            direction="above",
            threshold=0.5,
            source="last_trade",
            once=False,
            triggered=True,
            last_value=0.6,
        )
        harness = AlertHarness(alert, 0.49)

        with patch("app.save_config") as save_config:
            App._eval_alerts_for_token(harness, "token-1")

        self.assertFalse(alert.triggered)
        self.assertEqual(harness.fired, [])
        self.assertTrue(harness.refreshed)
        save_config.assert_called_once()

    def test_copy_trade_simulation_uses_best_ask_slippage_and_max_usdc_cap(self) -> None:
        harness = CopyHarness()
        item = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "token-1234567890",
            "timestamp": int(time.time()),
            "size": "100",
            "price": "0.45",
        }

        App._copy_trade_from_activity(harness, item)

        kind, message = harness.ui_queue.get_nowait()
        self.assertEqual(kind, "log")
        self.assertIn("[copy SIM] BUY", message)
        self.assertIn("size=9.6154", message)
        self.assertIn("price<= 0.5200", message)

    def test_copy_trade_live_fails_closed_for_clob_v2_before_any_side_effect(self) -> None:
        harness = CopyHarness()
        harness.cfg.copytrading.live = True
        harness.polymarket_adapter.geoblock_error = AssertionError("geoblock must not run")
        harness._get_trader = lambda: self.fail("legacy trader must not be created")
        persisted_dispatches = []

        outcome = App._copy_trade_from_activity(
            harness,
            {
                "proxyWallet": WALLET,
                "side": "BUY",
                "asset": "token-1234567890",
                "timestamp": int(time.time()),
                "size": "1",
                "price": "0.45",
            },
            market_id="polymarket",
            before_live_dispatch=persisted_dispatches.append,
        )

        self.assertEqual(outcome.state, "rejected")
        self.assertEqual(outcome.code, "polymarket_clob_v2_migration_required")
        self.assertIn("CLOB V2", outcome.message)
        self.assertEqual(persisted_dispatches, [])
        self.assertEqual(harness.polymarket_adapter.preflight_calls, [])

    def test_source_market_change_is_retryable_and_never_uses_selected_adapter(self) -> None:
        harness = CopyHarness()
        harness.cfg.selected_market_id = "kalshi"
        item = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "token-1234567890",
            "size": "1",
            "price": "0.45",
        }

        mismatch = App._copy_trade_from_activity(harness, item, market_id="polymarket")
        harness.cfg.selected_market_id = "polymarket"
        replayed = App._copy_trade_from_activity(harness, item, market_id="polymarket")

        self.assertEqual(mismatch.state, "retryable")
        self.assertEqual(mismatch.code, "source_market_mismatch")
        self.assertEqual(replayed.state, "completed")

    def test_staged_paper_policy_cannot_escalate_to_live_or_larger_order(self) -> None:
        for mutation in (
            lambda settings: setattr(settings, "live", True),
            lambda settings: setattr(settings, "scale", settings.scale + 0.25),
            lambda settings: setattr(settings, "max_usdc_per_trade", settings.max_usdc_per_trade + 5.0),
        ):
            with self.subTest(mutation=mutation):
                harness = CopyHarness()
                staged_policy = _copy_execution_policy(harness.cfg.copytrading)
                mutation(harness.cfg.copytrading)
                harness._get_polymarket_adapter = lambda: self.fail(
                    "policy drift must fail before adapter access"
                )

                outcome = App._copy_trade_from_activity(
                    harness,
                    {
                        "proxyWallet": WALLET,
                        "side": "BUY",
                        "asset": "token-1",
                        "size": "1",
                        "price": "0.45",
                    },
                    market_id="polymarket",
                    execution_policy=staged_policy,
                    staged_at=int(time.time()),
                )

                self.assertEqual(outcome.state, "retryable")
                self.assertEqual(outcome.code, "execution_policy_changed")

    def test_legacy_replay_without_bound_policy_requires_manual_reconciliation(self) -> None:
        harness = CopyHarness()
        harness._get_polymarket_adapter = lambda: self.fail("missing policy must fail before adapter access")

        outcome = App._copy_trade_from_activity(
            harness,
            {"proxyWallet": WALLET, "side": "BUY", "asset": "token-1", "size": "1"},
            market_id="polymarket",
            execution_policy={},
            staged_at=int(time.time()),
        )

        self.assertEqual(outcome.state, "ambiguous")
        self.assertEqual(outcome.code, "execution_policy_missing")

    def test_stale_outbox_and_live_source_timestamps_fail_before_adapter_access(self) -> None:
        harness = CopyHarness()
        current_policy = _copy_execution_policy(harness.cfg.copytrading)
        harness._get_polymarket_adapter = lambda: self.fail("stale replay must fail before adapter access")
        stale_outbox = App._copy_trade_from_activity(
            harness,
            {"proxyWallet": WALLET, "side": "BUY", "asset": "token-1", "size": "1"},
            market_id="polymarket",
            execution_policy=current_policy,
            staged_at=int(time.time()) - 301,
        )

        harness.cfg.copytrading.live = True
        live_policy = _copy_execution_policy(harness.cfg.copytrading)
        for timestamp in (None, int(time.time()) - 301, (int(time.time()) - 301) * 1000):
            with self.subTest(timestamp=timestamp):
                item = {
                    "proxyWallet": WALLET,
                    "side": "BUY",
                    "asset": "token-1",
                    "size": "1",
                }
                if timestamp is not None:
                    item["timestamp"] = timestamp
                outcome = App._copy_trade_from_activity(
                    harness,
                    item,
                    market_id="polymarket",
                    execution_policy=live_policy,
                    staged_at=int(time.time()),
                )
                self.assertEqual(outcome.state, "rejected")
                self.assertIn(outcome.code, {"live_activity_timestamp_missing", "live_activity_timestamp_stale"})

        self.assertEqual(stale_outbox.code, "copy_activity_stale")

    def test_confirmed_not_dispatched_reauthorizes_stale_live_source(self) -> None:
        harness = CopyHarness()
        harness.cfg.copytrading.live = True
        old_timestamp = int(time.time()) - _COPY_ACTIVITY_MAX_REPLAY_AGE_SECONDS - 60
        activity = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "token-1",
            "size": "1",
            "price": "0.45",
            "timestamp": old_timestamp,
        }
        entry = CopyActivityOutboxEntry(
            id="manual-replay",
            watch_id="watch-1",
            activity_key="tx:manual-replay",
            activity=activity,
            market_id="polymarket",
            execution_policy=_copy_execution_policy(harness.cfg.copytrading),
            state="ambiguous",
            created_at=old_timestamp,
            updated_at=old_timestamp,
        )
        harness.cfg.copy_activity_outbox = [entry]
        harness.cfg.reconcile_ambiguous_copy_activity(
            entry.id,
            "confirmed_not_dispatched",
        )
        trader_calls = []
        persisted_dispatches = []

        class Trader:
            def place_limit_order(self, **kwargs):
                trader_calls.append(kwargs)
                return {"status": "accepted"}

        harness._get_trader = Trader
        outcome = App._copy_trade_from_activity(
            harness,
            activity,
            market_id=entry.market_id,
            execution_policy=entry.execution_policy,
            staged_at=entry.created_at,
            replay_authorized_at=entry.replay_authorized_at,
            before_live_dispatch=persisted_dispatches.append,
        )

        self.assertEqual(entry.state, "retryable")
        self.assertGreater(entry.replay_authorized_at, old_timestamp)
        self.assertEqual(outcome.state, "rejected")
        self.assertEqual(outcome.code, "polymarket_clob_v2_migration_required")
        self.assertEqual(persisted_dispatches, [])
        self.assertEqual(trader_calls, [])

    def test_live_copy_requires_fresh_executable_side_orderbook_quote(self) -> None:
        harness = CopyHarness()
        harness.cfg.copytrading.live = True
        harness.polymarket_adapter.get_orderbook = lambda contract_id: OrderBookSnapshot(
            market_id="polymarket",
            contract_id=contract_id,
            bids=[],
            asks=[],
        )
        harness._get_trader = lambda: self.fail("missing quote must fail before trader access")
        callbacks = []

        outcome = App._copy_trade_from_activity(
            harness,
            {
                "proxyWallet": WALLET,
                "side": "BUY",
                "asset": "token-1",
                "size": "1",
                "timestamp": int(time.time()),
            },
            market_id="polymarket",
            before_live_dispatch=callbacks.append,
        )

        self.assertEqual(outcome.state, "retryable")
        self.assertEqual(outcome.code, "live_quote_unavailable")
        self.assertEqual(callbacks, [])

    def test_retryable_failure_does_not_commit_copy_conflict_reservation(self) -> None:
        other_wallet = "0x" + "c" * 40
        harness = CopyHarness()
        harness.cfg.copytrading.follow_wallets = [WALLET, other_wallet]
        first = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "token-1234567890",
            "size": "1",
            "price": "0.45",
            "timestamp": 100,
            "slug": "market",
            "outcome": "Yes",
        }
        second = {**first, "proxyWallet": other_wallet, "timestamp": 101}
        harness.polymarket_adapter.place_paper_order = lambda _order: (_ for _ in ()).throw(
            OSError("temporary paper ledger failure")
        )

        failed = App._copy_trade_from_activity(harness, first, market_id="polymarket")
        harness.polymarket_adapter.place_paper_order = lambda order: PaperOrderResult(
            market_id=order.market_id,
            contract_id=order.contract_id,
            accepted=True,
            message="accepted",
        )
        accepted = App._copy_trade_from_activity(harness, second, market_id="polymarket")

        self.assertEqual(failed.state, "retryable")
        self.assertEqual(accepted.state, "completed")
        self.assertNotEqual(accepted.code, "conflict_guard")

    def test_copy_trade_conflict_guard_skips_duplicate_followed_wallet_signal(self) -> None:
        other_wallet = "0x" + "c" * 40
        harness = CopyHarness()
        harness.cfg.copytrading.follow_wallets = [WALLET, other_wallet]
        first = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "token-1234567890",
            "size": "2",
            "price": "0.45",
            "timestamp": 100,
            "slug": "market",
            "outcome": "Yes",
        }
        duplicate = {**first, "proxyWallet": other_wallet, "timestamp": 101}

        App._copy_trade_from_activity(harness, first)
        App._copy_trade_from_activity(harness, duplicate)

        first_kind, first_message = harness.ui_queue.get_nowait()
        second_kind, second_message = harness.ui_queue.get_nowait()
        self.assertEqual(first_kind, "log")
        self.assertIn("[copy SIM] BUY", first_message)
        self.assertEqual(second_kind, "log")
        self.assertIn("Conflict guard skipped", second_message)

    def test_wallet_activity_checkpoint_is_persisted_before_copy_handling(self) -> None:
        class QueueHarness:
            def __init__(self) -> None:
                watch = WalletWatch(id="watch-1", wallet=WALLET, display_name="tracked")
                self.cfg = AppConfig(wallets=[watch])
                self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
                self.task = WalletActivityTask(
                    "watch-1",
                    {"timestamp": 100, "transactionHash": "tx-1"},
                    "tx:tx-1",
                )
                self.ui_queue.put(("wallet_activity", self.task))
                self._shutdown_started = True
                self.events: list[tuple] = []

            def _handle_wallet_activity(self, watch_id, item, **_kwargs) -> CopyActivityOutcome:
                self.events.append(("handled", watch_id, item))
                return CopyActivityOutcome("completed", "test_handled", "Handled by test harness.")

            def log(self, message: str) -> None:
                self.events.append(("log", message))

        harness = QueueHarness()

        with patch("app.save_config", side_effect=lambda cfg: harness.events.append(("saved", cfg))):
            App._process_queue(harness)

        self.assertEqual(harness.events[0], ("saved", harness.cfg))
        self.assertEqual(harness.events[1][0], "handled")
        self.assertEqual(harness.events[2], ("saved", harness.cfg))
        self.assertTrue(harness.task.completion.get_nowait())
        self.assertEqual(harness.cfg.wallets[0].seen_activity_keys, ["tx:tx-1"])
        self.assertEqual(harness.cfg.copy_activity_outbox[0].state, "completed")

    def test_wallet_activity_checkpoint_and_pending_outbox_are_staged_in_one_save(self) -> None:
        class QueueHarness:
            def __init__(self) -> None:
                self.cfg = AppConfig(wallets=[WalletWatch(id="watch-1", wallet=WALLET)])
                self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
                self.task = WalletActivityTask(
                    "watch-1",
                    {"timestamp": 100, "transactionHash": "tx-1"},
                    "tx:tx-1",
                    "polymarket",
                )
                self.ui_queue.put(("wallet_activity", self.task))
                self._shutdown_started = True

            def _handle_wallet_activity(self, *_args, **_kwargs) -> CopyActivityOutcome:
                return CopyActivityOutcome("completed", "handled", "Handled.")

            def log(self, _message: str) -> None:
                pass

        harness = QueueHarness()
        snapshots = []

        with patch(
            "app.save_config",
            side_effect=lambda cfg: snapshots.append(json.loads(json.dumps(cfg.to_dict()))),
        ):
            App._process_queue(harness)

        self.assertEqual(snapshots[0]["wallets"][0]["seen_activity_keys"], ["tx:tx-1"])
        self.assertEqual(snapshots[0]["copy_activity_outbox"][0]["state"], "pending")
        self.assertEqual(snapshots[0]["copy_activity_outbox"][0]["market_id"], "polymarket")
        self.assertEqual(snapshots[1]["copy_activity_outbox"][0]["state"], "completed")

    def test_wallet_activity_handler_failure_remains_durably_retryable(self) -> None:
        class QueueHarness:
            def __init__(self) -> None:
                self.cfg = AppConfig(wallets=[WalletWatch(id="watch-1", wallet=WALLET)])
                self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
                self.task = WalletActivityTask(
                    "watch-1",
                    {"timestamp": 100, "transactionHash": "tx-1"},
                    "tx:tx-1",
                    "polymarket",
                )
                self.ui_queue.put(("wallet_activity", self.task))
                self._shutdown_started = True
                self.logged = []

            def _handle_wallet_activity(self, *_args, **_kwargs):
                raise OSError("pre-dispatch failure")

            def log(self, message: str) -> None:
                self.logged.append(message)

        harness = QueueHarness()
        snapshots = []

        with patch(
            "app.save_config",
            side_effect=lambda cfg: snapshots.append(json.loads(json.dumps(cfg.to_dict()))),
        ):
            App._process_queue(harness)

        self.assertEqual(harness.cfg.copy_activity_outbox[0].state, "retryable")
        self.assertEqual(snapshots[-1]["copy_activity_outbox"][0]["state"], "retryable")
        self.assertTrue(harness.task.completion.get_nowait())

    def test_exception_after_dispatch_boundary_cannot_downgrade_ambiguous_state(self) -> None:
        class QueueHarness:
            def __init__(self) -> None:
                self.cfg = AppConfig(wallets=[WalletWatch(id="watch-1", wallet=WALLET)])
                self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
                self.task = WalletActivityTask(
                    "watch-1",
                    {"timestamp": 100, "transactionHash": "tx-1"},
                    "tx:tx-1",
                    "polymarket",
                )
                self.ui_queue.put(("wallet_activity", self.task))
                self._shutdown_started = True
                self.logged = []

            def _handle_wallet_activity(self, *_args, before_live_dispatch=None, **_kwargs):
                before_live_dispatch(
                    {
                        "market_id": "polymarket",
                        "contract_id": "token-1",
                        "side": "BUY",
                        "size": 1.0,
                        "limit_price": 0.5,
                        "tif": "FOK",
                    }
                )
                raise RuntimeError("unexpected post-boundary failure")

            def log(self, message: str) -> None:
                self.logged.append(message)

        harness = QueueHarness()
        snapshots = []

        with patch(
            "app.save_config",
            side_effect=lambda cfg: snapshots.append(json.loads(json.dumps(cfg.to_dict()))),
        ):
            App._process_queue(harness)

        self.assertEqual(harness.cfg.copy_activity_outbox[0].state, "ambiguous")
        self.assertEqual(snapshots[-1]["copy_activity_outbox"][0]["state"], "ambiguous")
        self.assertIn("manual reconciliation", " ".join(harness.logged).lower())

    def test_wallet_poller_replays_pending_in_order_before_new_activity(self) -> None:
        watch = WalletWatch(
            id="watch-1",
            wallet=WALLET,
            seen_activity_keys=["tx:old-1", "tx:old-2"],
            last_seen_ts=101,
        )
        cfg = AppConfig(
            wallets=[watch],
            copy_activity_outbox=[
                CopyActivityOutboxEntry(
                    id="entry-2",
                    watch_id=watch.id,
                    activity_key="tx:old-2",
                    activity={"timestamp": 101, "transactionHash": "old-2"},
                    market_id="polymarket",
                    created_at=2,
                ),
                CopyActivityOutboxEntry(
                    id="entry-1",
                    watch_id=watch.id,
                    activity_key="tx:old-1",
                    activity={"timestamp": 100, "transactionHash": "old-1"},
                    market_id="polymarket",
                    created_at=1,
                ),
            ],
        )
        processed = []

        class CompletingQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                result = super().put(item, *args, **kwargs)
                if item[0] == "wallet_activity":
                    task = item[1]
                    processed.append(task.checkpoint_key)
                    _apply_wallet_activity_checkpoint(cfg, task)
                    entry = next(
                        row for row in cfg.copy_activity_outbox if row.activity_key == task.checkpoint_key
                    )
                    entry.state = "completed"
                    task.acknowledge(True)
                return result

        ui_queue: "queue.Queue[tuple]" = CompletingQueue()
        poller = WalletPoller(ui_queue, cfg, poll_interval=0.01)
        responses = [
            [{"timestamp": 102, "transactionHash": "new-3", "slug": "m"}],
            [],
        ]

        def fake_get_activity(*_args, **_kwargs):
            if responses:
                response = responses.pop(0)
                if not responses:
                    poller.stop()
                return response
            poller.stop()
            return []

        with patch("app.data_api.get_activity", side_effect=fake_get_activity):
            poller._run()

        self.assertEqual(processed, ["tx:old-1", "tx:old-2", "tx:new-3"])

    def test_wallet_poller_surfaces_ambiguous_entry_without_replaying_it(self) -> None:
        watch = WalletWatch(
            id="watch-1",
            wallet=WALLET,
            seen_activity_keys=["tx:maybe"],
            last_seen_ts=100,
        )
        cfg = AppConfig(
            wallets=[watch],
            copy_activity_outbox=[
                CopyActivityOutboxEntry(
                    watch_id=watch.id,
                    activity_key="tx:maybe",
                    activity={"timestamp": 100, "transactionHash": "maybe"},
                    market_id="polymarket",
                    state="ambiguous",
                )
            ],
        )
        ui_queue: "queue.Queue[tuple]" = queue.Queue()
        poller = WalletPoller(ui_queue, cfg, poll_interval=0.01)

        def fake_get_activity(*_args, **_kwargs):
            poller.stop()
            return [{"timestamp": 100, "transactionHash": "maybe", "slug": "m"}]

        with patch("app.data_api.get_activity", side_effect=fake_get_activity):
            poller._run()

        drained = []
        while not ui_queue.empty():
            drained.append(ui_queue.get_nowait())
        self.assertFalse(any(item[0] == "wallet_activity" for item in drained))
        self.assertTrue(any("reconcile" in str(item[1]).lower() for item in drained if item[0] == "log"))

    def test_copy_outbox_retention_never_prunes_unresolved_entries(self) -> None:
        watch = WalletWatch(id="watch-1", wallet=WALLET)
        conclusive = [
            CopyActivityOutboxEntry(
                id=f"done-{index}",
                watch_id=watch.id,
                activity_key=f"tx:done-{index}",
                activity={"transactionHash": f"done-{index}"},
                state="completed",
                created_at=index,
                updated_at=index,
            )
            for index in range(501)
        ]
        ambiguous = CopyActivityOutboxEntry(
            id="ambiguous",
            watch_id=watch.id,
            activity_key="tx:ambiguous",
            activity={"transactionHash": "ambiguous"},
            state="ambiguous",
        )
        cfg = AppConfig(wallets=[watch], copy_activity_outbox=[*conclusive, ambiguous])

        checkpoint = _apply_wallet_activity_checkpoint(
            cfg,
            WalletActivityTask(
                watch.id,
                {"timestamp": 999, "transactionHash": "pending"},
                "tx:pending",
                "polymarket",
            ),
        )

        self.assertIsNotNone(checkpoint)
        self.assertLessEqual(len(cfg.copy_activity_outbox), 500)
        states = {entry.id: entry.state for entry in cfg.copy_activity_outbox}
        self.assertEqual(states["ambiguous"], "ambiguous")
        self.assertEqual(
            next(entry.state for entry in cfg.copy_activity_outbox if entry.activity_key == "tx:pending"),
            "pending",
        )

    def test_full_unresolved_outbox_applies_backpressure_without_checkpointing(self) -> None:
        watch = WalletWatch(id="watch-1", wallet=WALLET)
        cfg = AppConfig(
            wallets=[watch],
            copy_activity_outbox=[
                CopyActivityOutboxEntry(
                    id=f"pending-{index}",
                    watch_id=watch.id,
                    activity_key=f"tx:pending-{index}",
                    activity={"transactionHash": f"pending-{index}"},
                    state="retryable",
                )
                for index in range(500)
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "full of unresolved"):
            _apply_wallet_activity_checkpoint(
                cfg,
                WalletActivityTask(
                    watch.id,
                    {"timestamp": 999, "transactionHash": "new"},
                    "tx:new",
                    "polymarket",
                ),
            )

        self.assertEqual(len(cfg.copy_activity_outbox), 500)
        self.assertEqual(watch.seen_activity_keys, [])
        self.assertEqual(watch.last_seen_ts, 0)

    def test_wallet_poller_does_not_drop_cross_market_raw_key_collision(self) -> None:
        watch = WalletWatch(
            id="watch-1",
            wallet=WALLET,
            seen_activity_keys=["tx:same"],
            last_seen_ts=100,
            last_seen_tx="same",
        )
        cfg = AppConfig(
            selected_market_id="opinion_labs",
            wallets=[watch],
            copy_activity_outbox=[
                CopyActivityOutboxEntry(
                    watch_id=watch.id,
                    activity_key="tx:same",
                    activity={"timestamp": 100, "transactionHash": "same"},
                    market_id="polymarket",
                    state="completed",
                )
            ],
        )
        cfg.markets["opinion_labs"].enabled = True
        adapter = FakeOpinionActivityAdapter()
        adapter.list_activity = lambda wallet, limit=25: [
            {
                "timestamp": 100,
                "transactionHash": "same",
                "proxyWallet": wallet,
                "asset": "77:YES:0xyes",
                "side": "BUY",
                "size": 1,
                "price": 0.5,
            }
        ]
        processed = []

        class CompletingQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                result = super().put(item, *args, **kwargs)
                if item[0] == "wallet_activity":
                    task = item[1]
                    processed.append((task.market_id, task.checkpoint_key))
                    _apply_wallet_activity_checkpoint(cfg, task)
                    entry = _find_entry(task)
                    entry.state = "completed"
                    task.acknowledge(True)
                    poller.stop()
                return result

        def _find_entry(task):
            return next(
                entry
                for entry in cfg.copy_activity_outbox
                if entry.market_id == task.market_id and entry.activity_key == task.checkpoint_key
            )

        ui_queue: "queue.Queue[tuple]" = CompletingQueue()
        poller = WalletPoller(
            ui_queue,
            cfg,
            poll_interval=0.01,
            adapter_registry=FakeRegistry(adapter),
        )

        poller._run()

        self.assertEqual(processed, [("opinion_labs", "tx:same")])

    def test_wallet_activity_is_not_handled_when_checkpoint_persistence_fails(self) -> None:
        class QueueHarness:
            def __init__(self) -> None:
                watch = WalletWatch(id="watch-1", wallet=WALLET, display_name="tracked")
                self.cfg = AppConfig(wallets=[watch])
                self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
                self.task = WalletActivityTask(
                    "watch-1",
                    {"timestamp": 100, "transactionHash": "tx-1"},
                    "tx:tx-1",
                )
                self.ui_queue.put(("wallet_activity", self.task))
                self._shutdown_started = True
                self.handled: list[tuple] = []
                self.logged: list[str] = []

            def _handle_wallet_activity(self, watch_id, item) -> None:
                self.handled.append((watch_id, item))

            def log(self, message: str) -> None:
                self.logged.append(message)

        harness = QueueHarness()
        secret_detail = "disk error containing do-not-log"

        with patch("app.save_config", side_effect=OSError(secret_detail)):
            App._process_queue(harness)

        self.assertEqual(harness.handled, [])
        self.assertEqual(len(harness.logged), 1)
        self.assertIn("Refusing wallet activity", harness.logged[0])
        self.assertIn("OSError", harness.logged[0])
        self.assertNotIn(secret_detail, harness.logged[0])
        self.assertFalse(harness.task.completion.get_nowait())
        self.assertEqual(harness.cfg.wallets[0].last_seen_ts, 0)
        self.assertEqual(harness.cfg.wallets[0].seen_activity_keys, [])

    def test_wallet_poller_does_not_checkpoint_activity_before_enqueuing_it(self) -> None:
        cfg = AppConfig(wallets=[WalletWatch(wallet=WALLET, display_name="tracked")])
        snapshots: list[tuple[int, tuple[str, ...]]] = []

        class CheckpointQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                if item[0] == "wallet_activity":
                    watch = cfg.wallets[0]
                    snapshots.append((watch.last_seen_ts, tuple(watch.seen_activity_keys)))
                return super().put(item, *args, **kwargs)

        ui_queue: "queue.Queue[tuple]" = CheckpointQueue()
        poller = WalletPoller(ui_queue, cfg, poll_interval=0.01)

        def fake_get_activity(*_args, **_kwargs):
            poller.stop()
            return [{"timestamp": 100, "transactionHash": "tx1", "slug": "m"}]

        with patch("app.data_api.get_activity", side_effect=fake_get_activity):
            poller._run()

        self.assertEqual(snapshots, [(0, ())])
        self.assertEqual(cfg.wallets[0].last_seen_ts, 0)
        self.assertEqual(cfg.wallets[0].seen_activity_keys, [])

    def test_wallet_poller_deduplicates_same_timestamp_transactions(self) -> None:
        cfg = AppConfig(wallets=[WalletWatch(wallet=WALLET, display_name="tracked")])
        class CheckpointQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                result = super().put(item, *args, **kwargs)
                if item[0] == "wallet_activity":
                    task = item[1]
                    _apply_wallet_activity_checkpoint(cfg, task)
                    task.acknowledge(True)
                return result

        ui_queue: "queue.Queue[tuple]" = CheckpointQueue()
        poller = WalletPoller(ui_queue, cfg, poll_interval=0.01)
        responses = [
            [
                {"timestamp": 100, "transactionHash": "tx2", "slug": "m"},
                {"timestamp": 100, "transactionHash": "tx1", "slug": "m"},
            ],
            [
                {"timestamp": 100, "transactionHash": "tx2", "slug": "m"},
                {"timestamp": 100, "transactionHash": "tx1", "slug": "m"},
            ],
        ]
        calls = 0

        def fake_get_activity(*_args, **_kwargs):
            nonlocal calls
            if calls == len(responses):
                poller.stop()
                return []
            response = responses[calls]
            calls += 1
            return response

        with patch("app.data_api.get_activity", side_effect=fake_get_activity):
            poller._run()

        drained = []
        while not ui_queue.empty():
            drained.append(ui_queue.get_nowait())

        activity = [item for item in drained if item[0] == "wallet_activity"]
        self.assertEqual([item[1].activity["transactionHash"] for item in activity], ["tx1", "tx2"])
        self.assertEqual(cfg.wallets[0].last_seen_ts, 100)
        self.assertEqual(set(cfg.wallets[0].seen_activity_keys), {"tx:tx1", "tx:tx2"})

    def test_wallet_poller_uses_selected_market_activity_adapter(self) -> None:
        cfg = AppConfig(
            selected_market_id="opinion_labs",
            wallets=[WalletWatch(wallet=WALLET, display_name="tracked")],
        )
        cfg.markets["opinion_labs"].enabled = True
        class CheckpointQueue(queue.Queue):
            def put(self, item, *args, **kwargs):
                result = super().put(item, *args, **kwargs)
                if item[0] == "wallet_activity":
                    task = item[1]
                    _apply_wallet_activity_checkpoint(cfg, task)
                    task.acknowledge(True)
                return result

        ui_queue: "queue.Queue[tuple]" = CheckpointQueue()
        adapter = FakeOpinionActivityAdapter()
        registry = FakeRegistry(adapter)
        poller = WalletPoller(ui_queue, cfg, poll_interval=0.01, adapter_registry=registry)
        adapter.stopper = poller.stop

        poller._run()

        drained = []
        while not ui_queue.empty():
            drained.append(ui_queue.get_nowait())
        activity = [item for item in drained if item[0] == "wallet_activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0][1].activity["asset"], "77:YES:0xyes")
        self.assertEqual(adapter.activity_calls[0], (WALLET, 25))
        self.assertTrue(all(call[0] == "opinion_labs" for call in registry.calls))

    def test_opinion_desktop_copy_uses_selected_market_paper_adapter(self) -> None:
        harness = CopyHarness()
        harness.cfg.selected_market_id = "opinion_labs"
        harness.cfg.markets["opinion_labs"].enabled = True
        adapter = FakeOpinionActivityAdapter()
        harness.opinion_adapter = adapter
        harness.adapter_registry = FakeRegistry(adapter)
        item = {
            "proxyWallet": WALLET,
            "side": "BUY",
            "asset": "77:YES:0xyes",
            "size": "2",
            "price": "0.45",
        }

        App._copy_trade_from_activity(harness, item)

        self.assertEqual(len(adapter.paper_orders), 1)
        self.assertEqual(adapter.paper_orders[0].market_id, "opinion_labs")
        kind, message = harness.ui_queue.get_nowait()
        self.assertEqual(kind, "log")
        self.assertIn("[copy SIM] opinion_labs BUY", message)


if __name__ == "__main__":
    unittest.main()
