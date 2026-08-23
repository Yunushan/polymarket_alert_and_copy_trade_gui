from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.storage import load_config
from market_adapters import (
    MarketCandle,
    MarketContract,
    MarketEvent,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    PriceSnapshot,
)

import market_sentinel_cli
from polymarket.http_client import PolymarketHTTPError
from polymarket.leaderboard_state import LeaderboardStateStore


def run_cli_silent(args: list[str]) -> int:
    stdout = io.StringIO()
    with patch("sys.stdout", stdout):
        return market_sentinel_cli.main(args)


class MarketSentinelCliTests(unittest.TestCase):
    def test_market_read_commands_expose_normalized_adapter_operations(self) -> None:
        cfg = SimpleNamespace(selected_market_id="space")
        adapter = SimpleNamespace(
            list_events=lambda query, limit: [
                MarketEvent("space", "event-1", query or "event", status="open")
            ],
            list_contracts=lambda event_id: [
                MarketContract("space", "event-1:YES", event_id, "Yes", outcome="Yes")
            ],
            get_price=lambda contract: PriceSnapshot("space", contract, last=0.61, midpoint=0.61, source="fixture"),
            get_orderbook=lambda contract: OrderBookSnapshot(
                "space",
                contract,
                bids=[OrderBookLevel(0.6, 4.0)],
                asks=[OrderBookLevel(0.62, 3.0)],
            ),
            list_trades=lambda contract, **kwargs: [MarketTrade("space", contract, "trade-1", "BUY", 0.6, 2.0, 1700000000.0)],
            list_candles=lambda contract, **kwargs: [
                MarketCandle("space", contract, 1700000000.0, 0.59, 0.62, 0.58, 0.61, 10.0)
            ],
        )

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(["markets", "events", "--query", "launch", "--compact"]),
                0,
            )
            self.assertEqual(
                json.loads(stdout.getvalue())["events"][0]["title"],
                "launch",
            )

        commands = [
            (["markets", "contracts", "event-1", "--compact"], "contracts"),
            (["markets", "price", "event-1:YES", "--compact"], "price"),
            (["markets", "orderbook", "event-1:YES", "--compact"], "orderbook"),
            (["markets", "trades", "event-1:YES", "--before", "1700000010", "--compact"], "trades"),
            (["markets", "candles", "event-1:YES", "--resolution", "1h", "--from", "1700000000", "--compact"], "candles"),
        ]
        for command, key in commands:
            with self.subTest(command=command):
                stdout = io.StringIO()
                with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
                    "market_sentinel_cli._registry", return_value=SimpleNamespace()
                ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
                    "market_sentinel_cli.require_market_enabled"
                ), patch("sys.stdout", stdout):
                    self.assertEqual(market_sentinel_cli.main(command), 0)
                    self.assertIn(key, json.loads(stdout.getvalue()))

    def test_market_read_commands_reject_non_finite_time_bounds(self) -> None:
        with patch("market_sentinel_cli._load_cfg", return_value=SimpleNamespace(selected_market_id="space")), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("market_sentinel_cli.adapter_for_market"):
            self.assertEqual(market_sentinel_cli.main(["markets", "trades", "contract", "--before", "nan"]), 1)

    def test_market_account_command_exposes_allow_listed_recovery(self) -> None:
        cfg = SimpleNamespace(selected_market_id="gemini_titan")
        adapter = SimpleNamespace(
            account_recovery_operations=("positions",),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "positions": [{"symbol": "GEMI-BTC100K26-YES"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "positions",
                        "--event-ticker",
                        "BTC100K2026",
                        "--limit",
                        "10",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["operation"], "positions")
        self.assertEqual(payload["parameters"]["event_ticker"], "BTC100K2026")
        self.assertEqual(payload["data"]["positions"][0]["symbol"], "GEMI-BTC100K26-YES")

    def test_ibkr_account_and_order_management_commands_forward_documented_fields(self) -> None:
        cfg = SimpleNamespace(selected_market_id="ibkr_forecasttrader")
        account_calls = []
        order_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        def manage_orders(operation, **kwargs):
            order_calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            account_recovery_operations=("orders", "order_status"),
            account_recovery=account_recovery,
            order_management_operations=("cancel_order", "cancel_all_orders", "modify_order"),
            manage_orders=manage_orders,
        )
        common_patches = (
            patch("market_sentinel_cli._load_cfg", return_value=cfg),
            patch("market_sentinel_cli._registry", return_value=SimpleNamespace()),
            patch("market_sentinel_cli.adapter_for_market", return_value=adapter),
            patch("market_sentinel_cli.require_market_enabled"),
        )
        stdout = io.StringIO()
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "orders",
                        "--market",
                        "ibkr_forecasttrader",
                        "--status",
                        "filled",
                        "--historical",
                        "--compact",
                    ]
                ),
                0,
            )
        account_payload = json.loads(stdout.getvalue())
        self.assertEqual(account_calls, [("orders", {"filters": "filled", "force": True})])
        self.assertEqual(account_payload["data"]["operation"], "orders")

        instructions = {
            "conid": 721095497,
            "orderType": "LMT",
            "side": "BUY",
            "tif": "DAY",
            "quantity": 5,
            "price": 0.51,
        }
        stdout = io.StringIO()
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "modify_order",
                        "--market",
                        "ibkr_forecasttrader",
                        "--order-id",
                        "987654",
                        "--instructions",
                        json.dumps(instructions),
                        "--manual-indicator",
                        "false",
                        "--external-operator",
                        "desk-1",
                        "--confirm-order-management",
                        "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                        "--compact",
                    ]
                ),
                0,
            )
        order_payload = json.loads(stdout.getvalue())
        self.assertEqual(order_calls[0][0], "modify_order")
        self.assertEqual(order_calls[0][1]["order_id"], "987654")
        self.assertEqual(order_calls[0][1]["instructions"], instructions)
        self.assertEqual(order_calls[0][1]["manual_indicator"], "false")
        self.assertEqual(order_calls[0][1]["external_operator"], "desk-1")
        self.assertEqual(order_payload["data"]["operation"], "modify_order")

    def test_manifold_account_and_order_management_commands_forward_documented_fields(self) -> None:
        cfg = SimpleNamespace(selected_market_id="manifold")
        account_calls = []
        order_calls = []

        def account_recovery(operation, **kwargs):
            account_calls.append((operation, kwargs))
            return {"operation": operation, "parameters": kwargs}

        def manage_orders(operation, **kwargs):
            order_calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            account_recovery_operations=("account", "active_orders", "order_history"),
            account_recovery=account_recovery,
            order_management_operations=("cancel_order",),
            manage_orders=manage_orders,
        )
        common_patches = (
            patch("market_sentinel_cli._load_cfg", return_value=cfg),
            patch("market_sentinel_cli._registry", return_value=SimpleNamespace()),
            patch("market_sentinel_cli.adapter_for_market", return_value=adapter),
            patch("market_sentinel_cli.require_market_enabled"),
        )
        stdout = io.StringIO()
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "active_orders",
                        "--market",
                        "manifold",
                        "--contract",
                        "mf-binary-1:YES",
                        "--limit",
                        "20",
                        "--before",
                        "bet-open-1",
                        "--from",
                        "1760000000",
                        "--compact",
                    ]
                ),
                0,
            )
        account_payload = json.loads(stdout.getvalue())
        self.assertEqual(account_calls, [("active_orders", {
            "contract_id": "mf-binary-1:YES",
            "limit": 20,
            "before": "bet-open-1",
            "after": None,
            "before_time": None,
            "after_time": 1760000000.0,
        })])
        self.assertEqual(account_payload["data"]["operation"], "active_orders")

        stdout = io.StringIO()
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_order",
                        "--market",
                        "manifold",
                        "--order-id",
                        "bet-open-1",
                        "--confirm-order-management",
                        "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                        "--compact",
                    ]
                ),
                0,
            )
        order_payload = json.loads(stdout.getvalue())
        self.assertEqual(order_calls, [("cancel_order", {
            "order_id": "bet-open-1",
            "confirm_order_management": "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        })])
        self.assertEqual(order_payload["data"]["operation"], "cancel_order")

    def test_myriad_order_management_command_forwards_signed_mutations(self) -> None:
        cfg = SimpleNamespace(selected_market_id="myriad_markets")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "batch_cancel_orders", "cancel_all_orders", "batch_modify_orders"),
            manage_orders=manage_orders,
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        signed = {
            "order": {
                "trader": "0x1234567890123456789012345678901234567890",
                "marketId": "42",
                "outcomeId": 0,
                "side": 0,
                "amount": "1000000000000000000",
                "price": "500000000000000000",
                "minFillAmount": "0",
                "nonce": "1",
                "expiration": "0",
            },
            "signature": "0x" + "ab" * 65,
        }
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_order",
                        "--market",
                        "myriad_markets",
                        "--order-hash",
                        "0x" + "12" * 32,
                        "--instructions",
                        json.dumps(signed),
                        "--network-id",
                        "56",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_order")
        self.assertEqual(calls[0][1]["order_hash"], "0x" + "12" * 32)
        self.assertEqual(calls[0][1]["order"], signed["order"])
        self.assertEqual(calls[0][1]["signature"], signed["signature"])
        self.assertEqual(calls[0][1]["network_id"], "56")

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "batch_cancel_orders",
                        "--market",
                        "myriad_markets",
                        "--instructions",
                        json.dumps([signed]),
                        "--allow-partial",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "batch_cancel_orders")
        self.assertEqual(calls[1][1]["orders"], [signed])
        self.assertTrue(calls[1][1]["allow_partial"])
        self.assertNotIn("instructions", calls[1][1])

    def test_betfair_order_management_command_forwards_guarded_mutation_options(self) -> None:
        cfg = SimpleNamespace(selected_market_id="betfair_exchange")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_orders", "update_orders", "replace_orders"),
            manage_orders=manage_orders,
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_orders",
                        "--market",
                        "betfair_exchange",
                        "--exchange-market-id",
                        "1.234",
                        "--instructions",
                        '[{"bet_id":"bet-1","size_reduction":1.25}]',
                        "--customer-ref",
                        "cancel-1",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(calls[0][0], "cancel_orders")
        self.assertEqual(
            calls[0][1],
            {
                "market_id": "1.234",
                "instructions": [{"bet_id": "bet-1", "size_reduction": 1.25}],
                "customer_ref": "cancel-1",
            },
        )
        self.assertEqual(payload["parameters"]["market_id"], "1.234")

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "replace_orders",
                        "--market",
                        "betfair_exchange",
                        "--json",
                        '{"market_id":"1.234","instructions":[{"bet_id":"bet-1","new_price":2}],"market_version":7}',
                        "--async-request",
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "replace_orders")
        self.assertEqual(calls[1][1]["market_version"], 7)
        self.assertTrue(calls[1][1]["async_request"])

    def test_kalshi_order_management_command_forwards_v2_mutation_options(self) -> None:
        cfg = SimpleNamespace(selected_market_id="kalshi")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "batch_cancel_orders", "amend_order", "decrease_order"),
            manage_orders=manage_orders,
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "amend_order",
                        "--market",
                        "kalshi",
                        "--order-id",
                        "order-1",
                        "--ticker",
                        "KXTEST-YES",
                        "--side",
                        "bid",
                        "--price",
                        "0.44",
                        "--count",
                        "3",
                        "--confirm-order-management",
                        "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "amend_order")
        self.assertEqual(calls[0][1]["order_id"], "order-1")
        self.assertEqual(calls[0][1]["ticker"], "KXTEST-YES")
        self.assertEqual(calls[0][1]["count"], "3")
        self.assertEqual(
            calls[0][1]["confirm_order_management"],
            "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "batch_cancel_orders",
                        "--market",
                        "kalshi",
                        "--instructions",
                        '[{"order_id":"order-1"},{"order_id":"order-2","exchange_index":0}]',
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "batch_cancel_orders")
        self.assertEqual(
            calls[1][1]["orders"],
            [{"order_id": "order-1"}, {"order_id": "order-2", "exchange_index": 0}],
        )
        self.assertNotIn("instructions", calls[1][1])

    def test_gemini_order_management_command_forwards_single_and_batch_cancellations(self) -> None:
        cfg = SimpleNamespace(selected_market_id="gemini_titan")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "batch_cancel_orders"),
            manage_orders=manage_orders,
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_order",
                        "--market",
                        "gemini_titan",
                        "--order-id",
                        "106817811",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_order")
        self.assertEqual(calls[0][1]["order_id"], "106817811")
        self.assertEqual(calls[0][1]["confirm_order_management"], confirmation)

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "batch_cancel_orders",
                        "--market",
                        "gemini_titan",
                        "--instructions",
                        "[106817811,106817812]",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "batch_cancel_orders")
        self.assertEqual(calls[1][1]["orders"], [106817811, 106817812])
        self.assertEqual(calls[1][1]["confirm_order_management"], confirmation)
        self.assertNotIn("instructions", calls[1][1])

    def test_opinion_order_management_command_forwards_sdk_cancellation_filters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="opinion_labs")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "batch_cancel_orders", "cancel_all_orders"),
            manage_orders=manage_orders,
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_all_orders",
                        "--market",
                        "opinion_labs",
                        "--market-id",
                        "77",
                        "--side",
                        "BUY",
                        "--confirm-global-cancel",
                        "CANCEL ALL OPINION ORDERS",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_all_orders")
        self.assertEqual(calls[0][1]["market_id"], "77")
        self.assertEqual(calls[0][1]["side"], "BUY")
        self.assertEqual(calls[0][1]["confirm_global_cancel"], "CANCEL ALL OPINION ORDERS")
        self.assertEqual(calls[0][1]["confirm_order_management"], confirmation)

    def test_limitless_order_management_command_forwards_market_scoped_cancellation(self) -> None:
        cfg = SimpleNamespace(selected_market_id="limitless_exchange")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "batch_cancel_orders", "cancel_all_orders"),
            manage_orders=manage_orders,
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_all_orders",
                        "--market",
                        "limitless_exchange",
                        "--market-slug",
                        "doge-above-021652-sep-1-1200-utc",
                        "--confirm-global-cancel",
                        "CANCEL ALL LIMITLESS ORDERS",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_all_orders")
        self.assertEqual(calls[0][1]["market_slug"], "doge-above-021652-sep-1-1200-utc")
        self.assertEqual(calls[0][1]["confirm_global_cancel"], "CANCEL ALL LIMITLESS ORDERS")
        self.assertEqual(calls[0][1]["confirm_order_management"], confirmation)

    def test_matchbook_order_management_command_forwards_offer_mutation_fields(self) -> None:
        cfg = SimpleNamespace(selected_market_id="matchbook")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=(
                "cancel_offer",
                "cancel_offers",
                "cancel_all_offers",
                "edit_offer",
                "edit_offers",
            ),
            manage_orders=manage_orders,
        )
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_offers",
                        "--market",
                        "matchbook",
                        "--offer-ids",
                        "404,405",
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_offers")
        self.assertEqual(calls[0][1]["offer_ids"], "404,405")
        self.assertEqual(calls[0][1]["confirm_order_management"], confirmation)

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "edit_offers",
                        "--market",
                        "matchbook",
                        "--instructions",
                        '[{"id":404,"current-odds":1.5,"new-odds":2,"current-stake":5,"new-stake":6}]',
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "edit_offers")
        self.assertEqual(calls[1][1]["instructions"][0]["id"], 404)
        self.assertEqual(calls[1][1]["confirm_order_management"], confirmation)

    def test_hyperliquid_account_command_forwards_dex_and_history_limit(self) -> None:
        cfg = SimpleNamespace(selected_market_id="hyperliquid")
        adapter = SimpleNamespace(
            account_recovery_operations=("order_history",),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "orders": [{"coin": "#10"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "order_history",
                        "--market",
                        "hyperliquid",
                        "--limit",
                        "12",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["operation"], "order_history")
        self.assertEqual(payload["parameters"]["limit"], 12)

    def test_hyperliquid_order_management_command_forwards_signed_envelopes(self) -> None:
        cfg = SimpleNamespace(selected_market_id="hyperliquid")
        calls = []

        def manage_orders(operation, **kwargs):
            calls.append((operation, kwargs))
            return {"operation": operation, "request": kwargs}

        adapter = SimpleNamespace(
            order_management_operations=("cancel_order", "schedule_cancel"),
            manage_orders=manage_orders,
        )
        signed_cancel = {
            "action": {"type": "cancel", "cancels": [{"a": 100000000, "o": 123456789}]},
            "nonce": 1700000000000,
            "signature": "0x" + "ab" * 65,
        }
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_order",
                        "--market",
                        "hyperliquid",
                        "--instructions",
                        json.dumps(signed_cancel),
                        "--confirm-order-management",
                        confirmation,
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[0][0], "cancel_order")
        self.assertEqual(calls[0][1]["signed_action"], signed_cancel)
        self.assertEqual(calls[0][1]["confirm_order_management"], confirmation)

        signed_schedule = {
            "action": {"type": "scheduleCancel", "time": 1800000000000},
            "nonce": 1700000000001,
            "signature": "0x" + "cd" * 65,
        }
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "schedule_cancel",
                        "--market",
                        "hyperliquid",
                        "--instructions",
                        json.dumps(signed_schedule),
                        "--confirm-order-management",
                        confirmation,
                        "--confirm-global-cancel",
                        "SCHEDULE HYPERLIQUID CANCEL",
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(calls[1][0], "schedule_cancel")
        self.assertEqual(calls[1][1]["signed_action"], signed_schedule)
        self.assertEqual(calls[1][1]["confirm_global_cancel"], "SCHEDULE HYPERLIQUID CANCEL")

    def test_opinion_account_command_forwards_page_filters_and_order_id(self) -> None:
        cfg = SimpleNamespace(selected_market_id="opinion_labs")
        adapter = SimpleNamespace(
            account_recovery_operations=("order_history", "order_detail", "positions"),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "result": {"list": [{"orderId": "order-1"}]},
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "order_history",
                        "--page",
                        "2",
                        "--limit",
                        "20",
                        "--account-market-id",
                        "77",
                        "--chain-id",
                        "56",
                        "--status",
                        "1,2",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["parameters"], {"page": 2, "limit": 20, "market_id": "77", "chain_id": "56", "status": "1,2"})

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    ["markets", "account", "order_detail", "--order-id", "order-1", "--compact"]
                ),
                0,
            )
        self.assertEqual(json.loads(stdout.getvalue())["parameters"], {"order_id": "order-1"})

    def test_betfair_account_command_forwards_cleared_order_filters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="betfair_exchange")
        adapter = SimpleNamespace(
            account_recovery_operations=("active_orders", "cleared_orders", "funds", "account"),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "clearedOrders": [{"betId": "bet-1"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "cleared_orders",
                        "--market",
                        "betfair_exchange",
                        "--contract",
                        "1.234:101",
                        "--status",
                        "SETTLED",
                        "--limit",
                        "10",
                        "--offset",
                        "2",
                        "--from",
                        "1780308000",
                        "--to",
                        "1780394400",
                        "--compact",
                    ]
                ),
                0,
            )
        parameters = json.loads(stdout.getvalue())["parameters"]
        self.assertEqual(parameters["market_id"], "1.234")
        self.assertEqual(parameters["runner_id"], "101")
        self.assertEqual(parameters["bet_status"], "SETTLED")
        self.assertEqual(parameters["limit"], 10)
        self.assertEqual(parameters["offset"], 2)
        self.assertEqual(parameters["from_timestamp"], 1780308000.0)
        self.assertEqual(parameters["to_timestamp"], 1780394400.0)

    def test_betfair_account_command_forwards_active_order_and_funds_options(self) -> None:
        cfg = SimpleNamespace(selected_market_id="betfair_exchange")
        adapter = SimpleNamespace(
            account_recovery_operations=("active_orders", "cleared_orders", "funds", "account"),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "active_orders",
                        "--market",
                        "betfair_exchange",
                        "--contract",
                        "1.234:101",
                        "--status",
                        "EXECUTABLE",
                        "--order-by",
                        "BY_PLACE_TIME",
                        "--sort-dir",
                        "LATEST_TO_EARLIEST",
                        "--limit",
                        "8",
                        "--offset",
                        "3",
                        "--compact",
                    ]
                ),
                0,
            )
        active = json.loads(stdout.getvalue())["parameters"]
        self.assertEqual(active["market_id"], "1.234")
        self.assertEqual(active["contract_id"], "1.234:101")
        self.assertEqual(active["order_by"], "BY_PLACE_TIME")
        self.assertEqual(active["sort_dir"], "LATEST_TO_EARLIEST")

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    ["markets", "account", "funds", "--market", "betfair_exchange", "--wallet", "UK", "--compact"]
                ),
                0,
            )
        self.assertEqual(json.loads(stdout.getvalue())["parameters"], {"wallet": "UK"})

    def test_betfair_account_command_forwards_statement_and_currency_options(self) -> None:
        cfg = SimpleNamespace(selected_market_id="betfair_exchange")
        adapter = SimpleNamespace(
            account_recovery_operations=(
                "active_orders",
                "cleared_orders",
                "funds",
                "account",
                "statement",
                "currency_rates",
            ),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "statement",
                        "--market",
                        "betfair_exchange",
                        "--locale",
                        "en",
                        "--wallet",
                        "UK",
                        "--limit",
                        "12",
                        "--offset",
                        "4",
                        "--from",
                        "1780272000",
                        "--to",
                        "1780358400",
                        "--compact",
                    ]
                ),
                0,
            )
        statement = json.loads(stdout.getvalue())["parameters"]
        self.assertEqual(statement["locale"], "en")
        self.assertEqual(statement["limit"], 12)
        self.assertEqual(statement["offset"], 4)
        self.assertTrue(statement["include_item"])
        self.assertEqual(statement["wallet"], "UK")

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "currency_rates",
                        "--market",
                        "betfair_exchange",
                        "--from-currency",
                        "GBP",
                        "--compact",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(stdout.getvalue())["parameters"], {"from_currency": "GBP"})

    def test_matchbook_account_command_forwards_report_and_offer_filters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="matchbook")
        adapter = SimpleNamespace(
            account_recovery_operations=("settled_bets", "current_bets", "current_offers", "balance", "account"),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "data": {"operation": operation},
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "settled_bets",
                        "--market",
                        "matchbook",
                        "--account-sport-id",
                        "1",
                        "--account-event-id",
                        "101",
                        "--account-market-id",
                        "202",
                        "--limit",
                        "10",
                        "--offset",
                        "2",
                        "--from",
                        "1780344000",
                        "--to",
                        "1780347600",
                        "--account-odds-type",
                        "DECIMAL",
                        "--compact",
                    ]
                ),
                0,
            )
        parameters = json.loads(stdout.getvalue())["parameters"]
        self.assertEqual(parameters["sport_id"], "1")
        self.assertEqual(parameters["event_id"], "101")
        self.assertEqual(parameters["market_id"], "202")
        self.assertEqual(parameters["limit"], 10)
        self.assertEqual(parameters["offset"], 2)
        self.assertEqual(parameters["from_timestamp"], 1780344000.0)

        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "current_offers",
                        "--market",
                        "matchbook",
                        "--account-side",
                        "back",
                        "--account-offer-status",
                        "open,matched",
                        "--account-interval",
                        "30",
                        "--account-include-edits",
                        "--account-aggregation-type",
                        "average",
                        "--compact",
                    ]
                ),
                0,
            )
        parameters = json.loads(stdout.getvalue())["parameters"]
        self.assertEqual(parameters["side"], "back")
        self.assertEqual(parameters["status"], "open,matched")
        self.assertEqual(parameters["interval"], 30)
        self.assertTrue(parameters["include_edits"])
        self.assertEqual(parameters["aggregation_type"], "average")

    def test_kalshi_account_command_forwards_signed_read_parameters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="kalshi")
        adapter = SimpleNamespace(
            account_recovery_operations=("fills",),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "fills": [{"fill_id": "fill-1"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "fills",
                        "--ticker",
                        "KXTEST-YES",
                        "--order-id",
                        "order-1",
                        "--historical",
                        "--limit",
                        "12",
                        "--from",
                        "1700000000",
                        "--to",
                        "1700000100",
                        "--subaccount",
                        "2",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["operation"], "fills")
        self.assertEqual(payload["parameters"]["ticker"], "KXTEST-YES")
        self.assertEqual(payload["parameters"]["order_id"], "order-1")
        self.assertTrue(payload["parameters"]["historical"])
        self.assertEqual(payload["parameters"]["limit"], 12)
        self.assertEqual(payload["parameters"]["subaccount"], 2)

    def test_limitless_account_command_forwards_delegated_read_parameters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="limitless_exchange")
        adapter = SimpleNamespace(
            account_recovery_operations=("user_orders",),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "orders": [{"order_id": "order-1"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "user_orders",
                        "--market-slug",
                        "doge-above-021652-sep-1-1200-utc",
                        "--on-behalf-of",
                        "profile-123",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["operation"], "user_orders")
        self.assertEqual(
            payload["parameters"],
            {
                "on_behalf_of": "profile-123",
                "market_slug": "doge-above-021652-sep-1-1200-utc",
            },
        )
        self.assertEqual(payload["data"]["orders"][0]["order_id"], "order-1")

    def test_xmarket_account_command_forwards_bounded_market_order_parameters(self) -> None:
        cfg = SimpleNamespace(selected_market_id="xmarket")
        adapter = SimpleNamespace(
            account_recovery_operations=("positions", "user_orders", "market_orders"),
            account_recovery=lambda operation, **kwargs: {
                "operation": operation,
                "parameters": kwargs,
                "items": [{"id": "xorder-1"}],
            },
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "market_orders",
                        "--market",
                        "xmarket",
                        "--account-market-id",
                        "market-1",
                        "--status",
                        "open",
                        "--page",
                        "2",
                        "--limit",
                        "25",
                        "--compact",
                    ]
                ),
                0,
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["operation"], "market_orders")
        self.assertEqual(
            payload["parameters"],
            {"status": "open", "page": 2, "limit": 25, "market_id": "market-1"},
        )
        self.assertEqual(payload["data"]["items"][0]["id"], "xorder-1")

    def test_smarkets_account_and_order_management_commands_forward_allow_listed_fields(self) -> None:
        cfg = SimpleNamespace(selected_market_id="smarkets")
        adapter = SimpleNamespace(
            account_recovery_operations=("order_history", "account"),
            order_management_operations=("cancel_order", "cancel_orders"),
            account_recovery=lambda operation, **kwargs: {"operation": operation, "parameters": kwargs},
            manage_orders=lambda operation, **kwargs: {"operation": operation, "parameters": kwargs},
        )
        stdout = io.StringIO()
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "account",
                        "order_history",
                        "--market",
                        "smarkets",
                        "--status",
                        "created,filled",
                        "--limit",
                        "25",
                        "--compact",
                    ]
                ),
                0,
            )
        account_payload = json.loads(stdout.getvalue())
        self.assertEqual(account_payload["parameters"], {"status": "created,filled", "limit": 25})

        stdout.seek(0)
        stdout.truncate(0)
        with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
            "market_sentinel_cli._registry", return_value=SimpleNamespace()
        ), patch("market_sentinel_cli.adapter_for_market", return_value=adapter), patch(
            "market_sentinel_cli.require_market_enabled"
        ), patch("sys.stdout", stdout):
            self.assertEqual(
                market_sentinel_cli.main(
                    [
                        "markets",
                        "manage-orders",
                        "cancel_orders",
                        "--market",
                        "smarkets",
                        "--market-id",
                        "market-1",
                        "--confirm-order-management",
                        "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
                        "--compact",
                    ]
                ),
                0,
            )
        order_payload = json.loads(stdout.getvalue())
        self.assertEqual(order_payload["operation"], "cancel_orders")
        self.assertEqual(order_payload["parameters"]["market_id"], "market-1")
        self.assertEqual(
            order_payload["parameters"]["confirm_order_management"],
            "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS",
        )

    def test_polymarket_leaderboard_cli_builds_unlimited_scan_params(self) -> None:
        parser = market_sentinel_cli.build_parser()
        args = parser.parse_args(
            [
                "polymarket-leaderboard",
                "--sort",
                "roi",
                "--returned",
                "unlimited",
                "--scanned",
                "all",
                "--compute-mdd",
                "--fast-scan",
                "--mdd-scan",
                "0",
                "--max-mdd-pct",
                "20",
                "--param",
                "mdd_cache_ttl_seconds=120",
            ]
        )

        params = market_sentinel_cli.build_polymarket_leaderboard_params(args)

        self.assertEqual(params["sort"], ["roi_pct"])
        self.assertEqual(params["limit"], ["unlimited"])
        self.assertEqual(params["scan_limit"], ["all"])
        self.assertEqual(params["compute_mdd"], ["true"])
        self.assertEqual(params["fast_scan"], ["true"])
        self.assertEqual(params["mdd_scan_limit"], ["0"])
        self.assertEqual(params["max_mdd_pct"], ["20"])
        self.assertEqual(params["mdd_cache_ttl_seconds"], ["120"])
        self.assertEqual(params["scan_retry_attempts"], ["5"])
        self.assertEqual(params["scan_retry_delay_seconds"], ["30"])

    def test_polymarket_leaderboard_cli_runs_headless_json_output(self) -> None:
        payload = {
            "rows": [{"rank": 1, "display_name": "alpha", "wallet": "0xabc", "roi_pct": 12.5}],
            "counts": {"returned": 1, "filtered": 1, "scanned": 5, "mdd_computed": 0},
            "warnings": [],
        }

        stdout = io.StringIO()
        with patch("market_sentinel_cli.polymarket_leaderboard_payload", return_value=payload) as mock_payload, patch(
            "sys.stdout",
            stdout,
        ):
            exit_code = market_sentinel_cli.main(
                [
                    "polymarket-leaderboard",
                    "--returned",
                    "all",
                    "--scanned",
                    "all",
                    "--format",
                    "json",
                    "--quiet",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["counts"]["scanned"], 5)
        called_params = mock_payload.call_args.args[0]
        self.assertEqual(called_params["limit"], ["all"])
        self.assertEqual(called_params["scan_limit"], ["all"])
        self.assertIsNone(mock_payload.call_args.kwargs["progress_callback"])

    def test_polymarket_leaderboard_progress_logs_runtime_data(self) -> None:
        stderr = io.StringIO()
        emit = market_sentinel_cli._progress_printer(True, started_at=time.monotonic() - 10)

        with patch("sys.stderr", stderr):
            emit(
                {
                    "phase": "leaderboard",
                    "percent": 12.5,
                    "scanned": 100,
                    "scan_limit": 1000,
                    "scan_limit_unlimited": False,
                    "mdd_attempted": 0,
                    "mdd_total": 0,
                    "message": "Scanning leaderboard rows 100/1000.",
                }
            )

        line = stderr.getvalue()
        self.assertIn("status=running", line)
        self.assertIn("elapsed=", line)
        self.assertIn("phase=leaderboard", line)
        self.assertIn("percent=12.5%", line)
        self.assertIn("scan_rate=", line)
        self.assertIn("eta=", line)
        self.assertIn("Scanning leaderboard rows 100/1000.", line)

    def test_disk_backed_leaderboard_can_resume_after_a_transient_http_failure(self) -> None:
        parser = market_sentinel_cli.build_parser()
        args = parser.parse_args(
            [
                "polymarket-leaderboard",
                "--state-db",
                "data/leaderboard.sqlite3",
                "--resume-on-failure",
                "--resume-backoff-seconds",
                "1",
                "--quiet",
            ]
        )
        transient_error = PolymarketHTTPError("ssl eof", service="data", method="GET", url="https://data-api.polymarket.com")

        with patch("market_sentinel_cli._run_disk_backed_polymarket_leaderboard", side_effect=[transient_error, 0]) as mock_run, patch(
            "market_sentinel_cli.time.sleep"
        ) as mock_sleep:
            exit_code = market_sentinel_cli.run_polymarket_leaderboard(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_run.call_count, 2)
        self.assertTrue(args.resume)
        mock_sleep.assert_called_once_with(1.0)

    def test_resume_on_failure_requires_disk_backed_state(self) -> None:
        self.assertEqual(run_cli_silent(["polymarket-leaderboard", "--resume-on-failure", "--quiet"]), 1)

    def test_live_validation_cli_covers_local_report_review_workflows(self) -> None:
        parser = market_sentinel_cli.build_parser()
        commands = {
            ("polymarket-live-reports", "list"): "run_polymarket_live_reports_list",
            ("polymarket-live-reports", "store"): "run_polymarket_live_reports_store",
            ("polymarket-live-reports", "review", "report-key", "--format", "markdown"): "run_polymarket_live_reports_review",
            ("polymarket-live-decisions", "list"): "run_polymarket_live_decisions_list",
            ("polymarket-live-decisions", "record", "--report-key", "report-key", "--payload-hash", "payload", "--target-tier", "credential_live_verified", "--decision", "rejected", "--reviewer-note", "no evidence", "--review-bundle-hash", "bundle"): "run_polymarket_live_decisions_record",
            ("polymarket-promotion-proposal", "show"): "run_polymarket_live_proposal_show",
            ("polymarket-promotion-proposal", "snapshots", "diff", "snapshot-key"): "run_polymarket_live_snapshots_diff",
        }

        for command, expected in commands.items():
            with self.subTest(command=command):
                args = parser.parse_args(list(command))
                self.assertEqual(args.func.__name__, expected)

    def test_live_validation_cli_lists_reports_and_writes_markdown_review(self) -> None:
        stdout = io.StringIO()
        with patch("market_sentinel_cli.polymarket_live_validation_reports_payload", return_value={"entries": []}), patch("sys.stdout", stdout):
            exit_code = market_sentinel_cli.main(["polymarket-live-reports", "list", "--compact"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"entries": []})

        stdout = io.StringIO()
        review = {"bundle": {"review": "safe"}}
        with patch("market_sentinel_cli.polymarket_live_validation_report_review_payload", return_value=review), patch(
            "market_sentinel_cli.live_validation_report_review_markdown", return_value="# Review\n"
        ), patch("sys.stdout", stdout):
            exit_code = market_sentinel_cli.main(
                ["polymarket-live-reports", "review", "report-key", "--format", "markdown"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "# Review\n")

    def test_paper_marks_cli_refreshes_durable_sidecar_and_supports_mark_commands(self) -> None:
        parser = market_sentinel_cli.build_parser()
        commands = {
            ("paper", "marks", "refresh"): "run_paper_marks_refresh",
            ("paper", "marks", "refresh-selected", "--market", "kalshi", "--contract", "KX"): "run_paper_marks_refresh_selected",
            ("paper", "marks", "clear"): "run_paper_marks_clear",
            ("paper", "marks", "clear-selected", "--market", "kalshi", "--contract", "KX"): "run_paper_marks_clear_selected",
        }
        for command, expected in commands.items():
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args(list(command)).func.__name__, expected)

        cfg = SimpleNamespace(paper_trades=[])
        rows = [{"market_id": "kalshi", "contract_id": "KX", "net_size": 1.0}]
        marks = {("kalshi", "KX"): {"mark_price": 0.61, "source": "bid", "marked_at": 123}}
        with tempfile.TemporaryDirectory() as tmp:
            marks_path = Path(tmp) / "marks.json"
            stdout = io.StringIO()
            with patch("market_sentinel_cli._load_cfg", return_value=cfg), patch(
                "market_sentinel_cli.paper_position_rows", return_value=rows
            ), patch("market_sentinel_cli.refresh_paper_marks", return_value=(marks, [])), patch(
                "market_sentinel_cli.paper_payload", return_value={"summary": {"marked": 1}}
            ), patch("sys.stdout", stdout):
                exit_code = market_sentinel_cli.main(["paper", "marks", "refresh", "--marks-file", str(marks_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["paper"]["summary"]["marked"], 1)
            self.assertEqual(market_sentinel_cli._load_paper_marks(marks_path), marks)

    def test_paper_marks_sync_parent_directory_after_atomic_replace_on_posix(self) -> None:
        path = Path("state") / "marks.json"
        with patch("market_sentinel_cli.os.name", "posix"), patch(
            "market_sentinel_cli.os.open", return_value=41
        ) as open_directory, patch("market_sentinel_cli.os.fsync") as fsync, patch(
            "market_sentinel_cli.os.close"
        ) as close_directory:
            market_sentinel_cli._fsync_parent_directory(path)

        open_directory.assert_called_once_with(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        fsync.assert_called_once_with(41)
        close_directory.assert_called_once_with(41)

    def test_leaderboard_status_reads_existing_state_without_starting_a_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "leaderboard.sqlite3"
            pid_file = Path(tmp) / "scan.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            store = LeaderboardStateStore(state_path)
            try:
                store.prepare({"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "OVERALL"}, resume=False)
                store.record_page(
                    0,
                    50,
                    [{"rank": 1, "display_name": "leader", "wallet": "0x" + "1" * 40, "pnl_usd": 10.0, "volume_usd": 100.0, "roi_pct": 10.0, "trade_count": 1, "raw": {}}],
                )
            finally:
                store.close()

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = market_sentinel_cli.main(
                    ["polymarket-leaderboard-status", "--state-db", str(state_path), "--pid-file", str(pid_file), "--compact"]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["rows"], 1)
            self.assertEqual(payload["pages"], 1)
            self.assertEqual(payload["mdd_pending"], 1)
            self.assertEqual(payload["signature"]["remote_sort"], "PNL")
            self.assertEqual(payload["process"]["status"], "running")
            self.assertEqual(payload["process"]["pid"], os.getpid())

    def test_leaderboard_export_streams_partial_completed_mdd_rows_without_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "leaderboard.sqlite3"
            store = LeaderboardStateStore(state_path)
            try:
                store.prepare({"remote_sort": "PNL", "direction": "DESC", "period": "all", "category": "OVERALL"}, resume=False)
                store.record_page(
                    0,
                    2,
                    [
                        {"rank": 1, "display_name": "eligible", "wallet": "0x" + "1" * 40, "pnl_usd": 20.0, "volume_usd": 100.0, "roi_pct": 20.0, "trade_count": 1, "raw": {}},
                        {"rank": 2, "display_name": "pending", "wallet": "0x" + "2" * 40, "pnl_usd": 10.0, "volume_usd": 100.0, "roi_pct": 10.0, "trade_count": 1, "raw": {}},
                    ],
                )
                row = next(store.iter_mdd_candidates({}, sort="roi_pct", direction="DESC", limit=1))
                store.set_mdd(row["id"], {"mdd_usd": 5.0, "mdd_pct": 10.0, "mdd_method": "test", "mdd_pct_basis": "test"})
            finally:
                store.close()

            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard-export",
                        "--state-db",
                        str(state_path),
                        "--require-mdd",
                        "--max-mdd-pct",
                        "20",
                        "--format",
                        "json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["counts"]["returned"], 1)
            self.assertEqual(payload["rows"][0]["display_name"], "eligible")
            self.assertEqual(payload["counts"]["mdd_pending"], 1)

    def test_polymarket_leaderboard_cli_checkpoints_and_resumes_pages(self) -> None:
        payload = {
            "rows": [],
            "counts": {"returned": 0, "filtered": 0, "scanned": 1, "mdd_computed": 0},
            "warnings": [],
        }
        checkpoint_row = {
            "rank": 1,
            "proxyWallet": "0x" + "1" * 40,
            "pnl": "10",
            "volume": "100",
        }

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "leaderboard.checkpoint.jsonl"

            def fake_payload(_params, **kwargs):
                kwargs["leaderboard_page_callback"](0, 1, [checkpoint_row])
                return payload

            stdout = io.StringIO()
            with patch("market_sentinel_cli.polymarket_leaderboard_payload", side_effect=fake_payload), patch(
                "sys.stdout",
                stdout,
            ):
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard",
                        "--checkpoint",
                        str(checkpoint),
                        "--format",
                        "json",
                        "--quiet",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn('"type":"leaderboard_page"', checkpoint.read_text(encoding="utf-8"))

            stdout = io.StringIO()
            with patch("market_sentinel_cli.polymarket_leaderboard_payload", return_value=payload) as mock_payload, patch(
                "sys.stdout",
                stdout,
            ):
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard",
                        "--checkpoint",
                        str(checkpoint),
                        "--resume",
                        "--format",
                        "json",
                        "--quiet",
                    ]
                )

            self.assertEqual(exit_code, 0)
            called_params = mock_payload.call_args.args[0]
            self.assertEqual(called_params["scan_start_offset"], ["1"])
            self.assertEqual(mock_payload.call_args.kwargs["initial_raw_rows"], [checkpoint_row])
            self.assertTrue(callable(mock_payload.call_args.kwargs["leaderboard_page_callback"]))

    def test_polymarket_leaderboard_cli_state_db_streams_csv_and_resumes(self) -> None:
        raw_rows = [
            {"rank": 2, "proxyWallet": "0x" + "2" * 40, "pseudonym": "second", "pnl": "20", "volume": "200", "trades": 4},
            {"rank": 1, "proxyWallet": "0x" + "1" * 40, "pseudonym": "first", "pnl": "30", "volume": "100", "trades": 7},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_db = Path(tmp) / "leaderboard.sqlite3"
            output = Path(tmp) / "leaderboard.csv"

            def fake_scan(*_args, **kwargs):
                kwargs["page_callback"](0, 50, raw_rows)
                kwargs["page_callback"](2, 50, [])
                return [], False

            with patch("market_sentinel_cli._fetch_polymarket_leaderboard_scan_rows", side_effect=fake_scan) as mock_scan:
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard",
                        "--state-db",
                        str(state_db),
                        "--scanned",
                        "unlimited",
                        "--returned",
                        "unlimited",
                        "--format",
                        "csv",
                        "--output",
                        str(output),
                        "--quiet",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(state_db.exists())
            self.assertFalse(mock_scan.call_args.kwargs["retain_rows"])
            csv_lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(csv_lines), 3)
            self.assertIn("first", csv_lines[1])
            self.assertIn(",7,", csv_lines[1])

            with patch("market_sentinel_cli._fetch_polymarket_leaderboard_scan_rows") as mock_scan:
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard",
                        "--state-db",
                        str(state_db),
                        "--resume",
                        "--scanned",
                        "unlimited",
                        "--returned",
                        "unlimited",
                        "--format",
                        "csv",
                        "--output",
                        str(output),
                        "--quiet",
                    ]
                )

            self.assertEqual(exit_code, 0)
            mock_scan.assert_not_called()

    def test_polymarket_leaderboard_state_db_resumes_mdd_filtering(self) -> None:
        raw_rows = [
            {"rank": 1, "proxyWallet": "0x" + "1" * 40, "pnl": "30", "volume": "100"},
            {"rank": 2, "proxyWallet": "0x" + "2" * 40, "pnl": "20", "volume": "100"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state_db = Path(tmp) / "leaderboard.sqlite3"
            output = Path(tmp) / "leaderboard.json"

            def fake_scan(*_args, **kwargs):
                kwargs["page_callback"](0, 50, raw_rows)
                kwargs["page_callback"](2, 50, [])
                return [], False

            def fake_mdd(wallet, **_kwargs):
                return {
                    "mdd_usd": 10.0,
                    "mdd_pct": 10.0 if wallet.endswith("1") else 25.0,
                    "mdd_method": "public_data_historical_equity_curve_v2",
                    "mdd_pct_basis": "public equity basis",
                    "points": [{"timestamp": 1, "value": 1.0}],
                }

            with patch("market_sentinel_cli._fetch_polymarket_leaderboard_scan_rows", side_effect=fake_scan), patch(
                "market_sentinel_cli.polymarket_user_mdd_payload", side_effect=fake_mdd
            ) as mock_mdd:
                exit_code = market_sentinel_cli.main(
                    [
                        "polymarket-leaderboard",
                        "--state-db",
                        str(state_db),
                        "--scanned",
                        "unlimited",
                        "--returned",
                        "unlimited",
                        "--compute-mdd",
                        "--mdd-scan",
                        "unlimited",
                        "--max-mdd-pct",
                        "20",
                        "--format",
                        "json",
                        "--output",
                        str(output),
                        "--quiet",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(mock_mdd.call_count, 2)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["counts"]["returned"], 1)
            self.assertEqual(payload["rows"][0]["mdd_pct"], 10.0)
            self.assertNotIn("points", payload["rows"][0])
            self.assertEqual(payload["completion_reason"], "end_of_results")
            self.assertTrue(payload["source_enumeration_complete"])

    def test_config_and_market_cli_update_persisted_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / "config.json")

            self.assertEqual(
                run_cli_silent(
                    [
                        "config",
                        "set",
                        "--config",
                        config_path,
                        "--theme",
                        "dark",
                        "--design",
                        "sentinel_2027",
                        "--compact",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli_silent(
                    [
                        "markets",
                        "set",
                        "polymarket",
                        "--config",
                        config_path,
                        "--enabled",
                        "--live-trading-enabled",
                        "--no-live-trading-kill-switch",
                        "--live-trading-max-size",
                        "5",
                        "--compact",
                    ]
                ),
                0,
            )

            cfg = load_config(Path(config_path))
            self.assertEqual(cfg.theme, "dark")
            self.assertEqual(cfg.ui_design, "sentinel_2027")
            self.assertTrue(cfg.markets["polymarket"].enabled)
            self.assertEqual(cfg.markets["polymarket"].settings["live_trading_max_size"], 5.0)

    def test_wallet_and_copy_cli_manage_persisted_state(self) -> None:
        wallet = "0x" + "1" * 40
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / "config.json")

            self.assertEqual(
                run_cli_silent(
                    [
                        "wallets",
                        "add",
                        "--config",
                        config_path,
                        "--wallet",
                        wallet,
                        "--display-name",
                        "leader",
                        "--compact",
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli_silent(
                    [
                        "copy",
                        "set",
                        "--config",
                        config_path,
                        "--enabled",
                        "--follow-wallet",
                        wallet,
                        "--copy-percentage",
                        "25",
                        "--max-usdc-per-trade",
                        "10",
                        "--no-live",
                        "--compact",
                    ]
                ),
                0,
            )

            cfg = load_config(Path(config_path))
            self.assertEqual(cfg.wallets[0].wallet, wallet.lower())
            self.assertEqual(cfg.wallets[0].display_name, "leader")
            self.assertTrue(cfg.copytrading.enabled)
            self.assertEqual(cfg.copytrading.normalized_follow_wallets(), [wallet.lower()])
            self.assertEqual(cfg.copytrading.scale, 0.25)

    def test_paper_impact_cli_runs_without_gui(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stdout", stdout):
            exit_code = market_sentinel_cli.main(
                [
                    "paper",
                    "impact",
                    "--config",
                    str(Path(tmp) / "config.json"),
                    "--market",
                    "polymarket",
                    "--contract",
                    "token-1",
                    "--side",
                    "BUY",
                    "--size",
                    "3",
                    "--limit-price",
                    "0.42",
                    "--compact",
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["impact"]["projected_net"], 3.0)
        self.assertEqual(payload["impact"]["order_notional"], 1.26)

    def test_wallet_watch_cli_runs_one_headless_poll(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stdout", stdout):
            exit_code = market_sentinel_cli.main(
                [
                    "wallets",
                    "watch",
                    "--config",
                    str(Path(tmp) / "config.json"),
                    "--iterations",
                    "1",
                    "--interval",
                    "1",
                    "--compact",
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["polled_wallets"], 0)
        self.assertEqual(payload["activity"], [])

    def test_dependency_cli_skips_inactive_markers_and_import_fallbacks(self) -> None:
        self.assertIsNone(market_sentinel_cli._parse_requirement_entry("tomli>=2.0.0; python_version < '0'"))
        with patch("market_sentinel_cli.importlib_metadata.version", side_effect=market_sentinel_cli.importlib_metadata.PackageNotFoundError):
            with patch("market_sentinel_cli.importlib.import_module") as mock_import:
                fake_module = type("FakeModule", (), {"__version__": "1.9.0"})()
                mock_import.return_value = fake_module
                self.assertEqual(market_sentinel_cli._installed_version("websocket-client"), "1.9.0")
                mock_import.assert_called_with("websocket")

    def test_doctor_cli_reports_readiness_and_strict_warnings(self) -> None:
        safe_live_safety = {"status": "disabled", "selected_market_id": "polymarket", "blockers": ["live trading disabled"]}
        armed_live_safety = {"status": "armed", "selected_market_id": "polymarket", "blockers": []}
        dependencies = [{"package": "requests", "status": "ok"}]

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            with patch("market_sentinel_cli._dependency_rows", return_value=dependencies), patch(
                "market_sentinel_cli.health_payload",
                return_value={"frontend_build_available": True},
            ), patch("market_sentinel_cli.live_safety_payload", return_value=safe_live_safety):
                stdout = io.StringIO()
                with patch("sys.stdout", stdout):
                    exit_code = market_sentinel_cli.main(["doctor", "--config", str(config_path), "--frontend-dir", tmp])

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["counts"], {"fail": 0, "pass": 5, "warn": 0})
            self.assertEqual(payload["checks"][-1]["name"], "live_trading_safety")

            with patch("market_sentinel_cli._dependency_rows", return_value=dependencies), patch(
                "market_sentinel_cli.health_payload",
                return_value={"frontend_build_available": True},
            ), patch("market_sentinel_cli.live_safety_payload", return_value=armed_live_safety):
                self.assertEqual(run_cli_silent(["doctor", "--strict", "--config", str(config_path), "--frontend-dir", tmp]), 1)

    def test_doctor_cli_reports_corrupt_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text("{not-json", encoding="utf-8")
            stdout = io.StringIO()
            with patch("market_sentinel_cli._dependency_rows", return_value=[]), patch(
                "market_sentinel_cli.health_payload",
                return_value={"frontend_build_available": True},
            ), patch("sys.stdout", stdout):
                exit_code = market_sentinel_cli.main(["doctor", "--config", str(config_path), "--frontend-dir", tmp])

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["checks"][0]["name"], "configuration")
        self.assertEqual(payload["checks"][0]["status"], "fail")

    def test_full_app_cli_command_groups_are_registered(self) -> None:
        parser = market_sentinel_cli.build_parser()
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        for command in ("doctor", "config", "markets", "alerts", "wallets", "copy", "paper", "dependencies", "serve"):
            self.assertIn(command, help_text)

    def test_serve_forwards_frontend_directory_as_keyword(self) -> None:
        config_path = Path("custom-config.json")
        frontend_dir = Path("custom-frontend")
        args = SimpleNamespace(host="127.0.0.1", port=8766, config=config_path, frontend_dir=frontend_dir)
        with patch("market_sentinel_cli.run_server") as run_server:
            self.assertEqual(market_sentinel_cli.run_serve(args), 0)

        run_server.assert_called_once_with(
            "127.0.0.1",
            8766,
            config_path,
            frontend_dir=frontend_dir,
        )


if __name__ == "__main__":
    unittest.main()
