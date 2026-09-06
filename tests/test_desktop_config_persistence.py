from __future__ import annotations

import queue
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from tkinter import TclError
from unittest.mock import Mock, patch

import test_app_logic as helpers
from app import App, CopyActivityOutcome, WalletActivityTask
from core.models import AppConfig, PaperTradeRecord, PriceAlert, WalletWatch
from core.storage import load_config, save_config


class DesktopConfigPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.error = self.contexts.enter_context(patch("app.messagebox.showerror"))
        self.warning = self.contexts.enter_context(patch("app.messagebox.showwarning"))

    def bind_store(self, harness) -> None:
        save_config(harness.cfg, self.path)
        self.writer = self.contexts.enter_context(patch("app.save_config", side_effect=lambda cfg: save_config(cfg, self.path)))

    @staticmethod
    def armed_copy_harness():
        harness = helpers.AppLogicTests.copy_settings_harness()
        harness.ct_live_var.set(True)
        harness.status_var = helpers.FakeVar()
        return harness

    @staticmethod
    def armed_market_harness():
        harness = helpers.SafetyHarness()
        harness.cfg.markets["kalshi"].settings["live_trading_kill_switch"] = True
        harness.safety_live_enabled_var.set(True)
        harness.safety_live_confirmed_var.set(True)
        harness.safety_kill_switch_var.set(False)
        return harness

    def test_copy_precommit_failure_keeps_old_policy_and_blocks_later_writes(self) -> None:
        harness = self.armed_copy_harness()
        self.bind_store(harness)
        original = harness.cfg.copytrading
        before = self.path.read_bytes()
        with patch("core.storage.replace_file", side_effect=PermissionError("private disk detail")):
            App.save_copy_settings(harness)
        self.assertIs(harness.cfg.copytrading, original)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(harness.ct_live_var.get())
        self.assertTrue(harness._config_persistence_error)
        self.warning.assert_not_called()
        self.error.assert_called_once()
        self.assertNotIn("private disk detail", str(self.error.call_args))
        self.assertTrue(harness.ui_queue.empty())
        self.writer.reset_mock()
        App.save_copy_settings(harness)
        self.writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_market_precommit_failure_keeps_kill_switch_and_cache(self) -> None:
        harness = self.armed_market_harness()
        harness.polymarket_adapter = object()
        cached = harness.polymarket_adapter
        self.bind_store(harness)
        before = self.path.read_bytes()
        original = harness.cfg.markets["kalshi"]
        with patch("core.storage.replace_file", side_effect=OSError("disk full")):
            App.save_market_safety_settings(harness)
        self.assertIs(harness.cfg.markets["kalshi"], original)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(original.settings["live_trading_kill_switch"])
        self.assertFalse(harness.safety_live_enabled_var.get())
        self.assertTrue(harness.safety_kill_switch_var.get())
        self.assertIs(harness.polymarket_adapter, cached)
        self.assertTrue(harness._config_persistence_error)

    def test_invalid_caps_are_input_errors_without_a_persistence_pause(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity", "0", "-1", True, 0):
            with self.subTest(value=value):
                harness = self.armed_market_harness()
                harness.safety_max_size_var.set(value)
                before = harness.cfg.to_dict()
                with patch("app.save_config") as writer:
                    App.save_market_safety_settings(harness)
                writer.assert_not_called()
                self.assertEqual(harness.cfg.to_dict(), before)
                self.assertFalse(getattr(harness, "_config_persistence_error", ""))

    def test_stale_writer_does_not_publish_or_overwrite_newer_state(self) -> None:
        harness = self.armed_copy_harness()
        self.bind_store(harness)
        newer = load_config(self.path)
        newer.theme = "dark"
        save_config(newer, self.path)
        before = self.path.read_bytes()
        App.save_copy_settings(harness)
        self.assertFalse(harness.cfg.copytrading.live)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(harness._config_persistence_error)
        self.assertIn("restart", str(self.error.call_args).lower())

    def test_postcommit_failure_publishes_committed_snapshot_but_pauses_execution(self) -> None:
        harness = self.armed_copy_harness()
        self.bind_store(harness)
        root = harness.cfg
        journal = root.copy_activity_outbox
        with patch("core.storage._fsync_parent_directory", side_effect=OSError("directory sync failed")):
            App.save_copy_settings(harness)
        self.assertIs(harness.cfg, root)
        self.assertIs(root.copy_activity_outbox, journal)
        self.assertTrue(root.copytrading.live)
        self.assertEqual(root.to_dict(), load_config(self.path).to_dict())
        self.assertTrue(harness._config_persistence_error)
        self.assertIn("replaced", str(self.error.call_args).lower())
        self.warning.assert_not_called()
        result = App._copy_trade_from_activity(harness, {})
        self.assertEqual(result.code, "config_persistence_paused")
        self.writer.reset_mock()
        App.save_copy_settings(harness)
        self.writer.assert_not_called()

    def test_success_publishes_only_after_save_and_preserves_worker_references(self) -> None:
        harness = self.armed_copy_harness()
        self.bind_store(harness)
        root = harness.cfg
        worker = SimpleNamespace(cfg=root)
        original = root.copytrading
        journal = root.copy_activity_outbox

        def observe(candidate):
            self.assertIsNot(candidate, root)
            self.assertIs(root.copytrading, original)
            self.assertFalse(worker.cfg.copytrading.live)
            self.assertTrue(candidate.copytrading.live)
            save_config(candidate, self.path)

        self.writer.side_effect = observe
        App.save_copy_settings(harness)
        self.assertIs(harness.cfg, root)
        self.assertIs(worker.cfg, root)
        self.assertIs(root.copy_activity_outbox, journal)
        self.assertTrue(worker.cfg.copytrading.live)
        self.assertEqual(root.to_dict(), load_config(self.path).to_dict())
        self.warning.assert_called_once()
        self.error.assert_not_called()
        root.theme = "dark"
        save_config(root, self.path)
        self.assertEqual(load_config(self.path).theme, "dark")

    def test_destroyed_status_widget_cannot_hide_a_committed_save_error(self) -> None:
        harness = self.armed_copy_harness()
        harness.status_var = SimpleNamespace(set=Mock(side_effect=TclError("widget destroyed")))
        self.bind_store(harness)
        with patch("core.storage._fsync_parent_directory", side_effect=OSError("directory sync failed")):
            App.save_copy_settings(harness)
        self.assertEqual(harness.cfg.to_dict(), load_config(self.path).to_dict())
        self.assertTrue(harness.cfg.copytrading.live)
        self.assertTrue(harness._config_persistence_error)

    def test_market_success_invalidates_adapter_only_after_commit(self) -> None:
        harness = self.armed_market_harness()
        harness.polymarket_adapter = object()
        cached = harness.polymarket_adapter
        self.bind_store(harness)
        original = harness.cfg.markets

        def observe(candidate):
            self.assertIs(harness.cfg.markets, original)
            self.assertIs(harness.polymarket_adapter, cached)
            self.assertTrue(original["kalshi"].settings["live_trading_kill_switch"])
            save_config(candidate, self.path)

        self.writer.side_effect = observe
        App.save_market_safety_settings(harness)
        self.assertIsNone(harness.polymarket_adapter)
        self.assertFalse(harness.cfg.markets["kalshi"].settings["live_trading_kill_switch"])

    def test_failed_market_selection_restores_selection(self) -> None:
        harness = helpers.MarketSelectionHarness()
        self.bind_store(harness)
        original = harness.cfg.selected_market_id
        harness.market_var.set("Kalshi (kalshi)")
        with patch("core.storage.replace_file", side_effect=OSError("disk full")):
            App._on_market_change(harness)
        self.assertEqual(harness.cfg.selected_market_id, original)
        self.assertEqual(harness.market_var.get(), harness._market_label_for_id(original))
        self.assertTrue(harness.ui_queue.empty())

    def test_failed_follow_does_not_partially_add_watch_or_follow(self) -> None:
        harness = helpers.AnalyticsHarness()
        harness._selected_leaderboard_wallet = lambda: helpers.WALLET
        harness._selected_leaderboard_display_name = lambda: "Trader"
        self.bind_store(harness)
        before = self.path.read_bytes()
        with patch("core.storage.replace_file", side_effect=OSError("disk full")):
            App.follow_selected_leaderboard_for_copy_trading(harness)
        self.assertEqual(harness.cfg.wallets, [])
        self.assertEqual(harness.cfg.copytrading.normalized_follow_wallets(), [])
        self.assertEqual(harness.ct_follow_var.get(), "")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(harness.ui_queue.empty())
        self.assertNotIn("added to copy", harness.status_var.get().lower())

    def test_failed_wallet_alert_and_history_edits_do_not_mutate_shared_collections(self) -> None:
        actions = (
            App.toggle_selected_wallet, App.delete_selected_wallet,
            App.toggle_selected_alert, App.delete_selected_alert, App.clear_paper_history,
            lambda h: App._add_wallet_watch(h, "0x" + "a" * 40, "new watch"),
        )
        for action in actions:
            with self.subTest(action=action.__name__):
                harness = SimpleNamespace(
                    cfg=AppConfig(
                        wallets=[WalletWatch(id="watch", wallet=helpers.WALLET)],
                        alerts=[PriceAlert(id="alert", token_id="token", label="test", direction="above", threshold=0.5)],
                        paper_trades=[PaperTradeRecord(
                            market_id="polymarket", contract_id="token", side="BUY", size=1,
                            limit_price=0.5, accepted=True, message="paper test",
                        )],
                    ),
                    ui_queue=queue.Queue(), status_var=helpers.FakeVar(),
                    _selected_wallet_id=lambda: "watch", _selected_alert_id=lambda: "alert",
                    market_ws=SimpleNamespace(subscribe=Mock()),
                )
                before = harness.cfg.to_dict()
                with patch("app.save_config", side_effect=OSError("disk full")), patch("app.messagebox.askyesno", return_value=True):
                    action(harness)
                self.assertEqual(harness.cfg.to_dict(), before)
                self.assertTrue(harness._config_persistence_error)
                self.assertTrue(harness.ui_queue.empty())
                harness.market_ws.subscribe.assert_not_called()

    def test_paused_alerts_do_not_fire_or_change_durable_crossing_state(self) -> None:
        alert = PriceAlert(token_id="token", label="test", direction="above", threshold=0.5)
        harness = helpers.AlertHarness(alert, 0.8)
        harness._config_persistence_error = "Restart required"
        before = harness.cfg.to_dict()
        with patch("app.save_config") as writer:
            App._eval_alerts_for_contract(harness, "polymarket", "token")
        self.assertEqual(harness.cfg.to_dict(), before)
        self.assertEqual(harness.fired, [])
        writer.assert_not_called()

    def test_failed_paper_history_does_not_report_success_or_admit_more_orders(self) -> None:
        harness = SimpleNamespace(cfg=AppConfig(), status_var=helpers.FakeVar())
        order = SimpleNamespace(market_id="polymarket", contract_id="token", side="BUY", size=1, limit_price=0.5)
        result = SimpleNamespace(accepted=True, message="paper", filled_size=1, average_price=0.5, raw={})
        self.bind_store(harness)
        with patch("core.storage.replace_file", side_effect=OSError("disk full")):
            self.assertIsNone(App._record_paper_trade(harness, order, result))
        self.assertEqual(harness.cfg.paper_trades, [])
        self.assertEqual(load_config(self.path).paper_trades, [])
        with patch.object(App, "_paper_order_from_form") as build_order:
            App.submit_paper_order(harness)
        build_order.assert_not_called()

    def queue_harness(self):
        harness = SimpleNamespace(
            cfg=AppConfig(wallets=[WalletWatch(id="watch", wallet=helpers.WALLET)]),
            ui_queue=queue.Queue(), _shutdown_started=True, log=Mock(),
            _handle_wallet_activity=Mock(return_value=CopyActivityOutcome("completed", "test", "test")),
        )
        task = WalletActivityTask("watch", {"timestamp":100,"transactionHash":"tx"}, "tx:tx")
        harness.ui_queue.put(("wallet_activity", task))
        return harness, task

    def test_paused_queue_does_not_checkpoint_or_handle_more_activity(self) -> None:
        harness, task = self.queue_harness()
        harness._config_persistence_error = "Restart required"
        with patch("app.save_config") as writer:
            App._process_queue(harness)
        writer.assert_not_called()
        harness._handle_wallet_activity.assert_not_called()
        self.assertEqual(harness.cfg.copy_activity_outbox, [])
        self.assertFalse(task.completion.get_nowait())

    def test_committed_checkpoint_is_not_rolled_back_in_memory_after_fsync_failure(self) -> None:
        harness, task = self.queue_harness()
        self.bind_store(harness)
        with patch("core.storage._fsync_parent_directory", side_effect=OSError("directory sync failed")):
            App._process_queue(harness)
        self.assertEqual(harness.cfg.to_dict(), load_config(self.path).to_dict())
        self.assertEqual(harness.cfg.copy_activity_outbox[0].state, "pending")
        self.assertTrue(harness._config_persistence_error)
        harness._handle_wallet_activity.assert_not_called()
        self.assertFalse(task.completion.get_nowait())

    def test_dispatch_intent_sync_failure_never_dispatches_or_clears_ambiguity(self) -> None:
        harness, task = self.queue_harness()
        self.bind_store(harness)
        dispatched = Mock()

        def handle(*_args, before_live_dispatch, **_kwargs):
            with patch("core.storage._fsync_parent_directory", side_effect=OSError("directory sync failed")):
                before_live_dispatch({"market_id":"polymarket", "contract_id":"token", "side":"BUY", "size":1, "limit_price":0.5})
            dispatched()
            return CopyActivityOutcome("completed", "test", "test")

        harness._handle_wallet_activity.side_effect = handle
        App._process_queue(harness)
        dispatched.assert_not_called()
        self.assertTrue(harness._config_persistence_error)
        self.assertEqual(harness.cfg.copy_activity_outbox[0].state, "ambiguous")
        self.assertEqual(load_config(self.path).copy_activity_outbox[0].state, "ambiguous")
        self.assertEqual(self.writer.call_count, 2)
        self.assertTrue(task.completion.get_nowait())


if __name__ == "__main__":
    unittest.main()
