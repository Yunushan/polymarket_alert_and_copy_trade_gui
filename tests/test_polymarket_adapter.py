from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import PolymarketAdapter
from market_adapters.errors import MarketConfigurationError
from market_adapters.types import PaperOrderRequest


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "polymarket"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class PolymarketAdapterTests(unittest.TestCase):
    def test_list_contracts_maps_gamma_market_outcomes(self) -> None:
        adapter = PolymarketAdapter()
        market = load_fixture("market.json")
        with patch("market_adapters.polymarket.gamma.get_event_by_slug", return_value=None), patch(
            "market_adapters.polymarket.gamma.get_market_by_slug", return_value=market
        ):
            contracts = adapter.list_contracts("market-slug")

        self.assertEqual([c.contract_id for c in contracts], ["token-yes", "token-no"])
        self.assertEqual([c.outcome for c in contracts], ["Yes", "No"])
        self.assertEqual(contracts[0].title, "Will it happen?")
        self.assertEqual(contracts[0].status, "active")

    def test_list_contracts_maps_gamma_event_markets(self) -> None:
        adapter = PolymarketAdapter()
        event = load_fixture("event.json")
        with patch("market_adapters.polymarket.gamma.get_event_by_slug", return_value=event):
            contracts = adapter.list_contracts("event-slug")

        self.assertEqual(len(contracts), 2)
        self.assertEqual(contracts[0].event_id, "market-1")
        self.assertEqual(contracts[0].contract_id, "token-yes")
        self.assertEqual(contracts[1].outcome, "No")

    def test_get_orderbook_and_price_map_clob_payloads(self) -> None:
        adapter = PolymarketAdapter()
        book = load_fixture("orderbook.json")
        with patch("market_adapters.polymarket.clob_rest.get_book", return_value=book), patch(
            "market_adapters.polymarket.clob_rest.get_midpoint", return_value=0.62
        ), patch(
            "market_adapters.polymarket.clob_rest.get_last_trade_price", return_value=0.61
        ):
            orderbook = adapter.get_orderbook("token-yes")
            price = adapter.get_price("token-yes")

        self.assertEqual(orderbook.bids[0].price, 0.60)
        self.assertEqual(orderbook.bids[0].size, 12.0)
        self.assertEqual(orderbook.asks[0].price, 0.64)
        self.assertEqual(price.bid, 0.60)
        self.assertEqual(price.ask, 0.64)
        self.assertEqual(price.last, 0.61)
        self.assertEqual(price.midpoint, 0.62)

    def test_get_orderbook_filters_invalid_levels_and_sorts_book(self) -> None:
        adapter = PolymarketAdapter()
        book = {
            "bids": [
                {"price": "0.40", "size": "10"},
                {"price": "1.50", "size": "10"},
                {"price": "0.55", "size": "0"},
                {"price": "0.50", "size": "4"},
                "bad",
            ],
            "asks": [
                {"price": "0.70", "size": "8"},
                {"price": "-0.10", "size": "8"},
                {"price": "0.62", "size": "3"},
            ],
        }
        with patch("market_adapters.polymarket.clob_rest.get_book", return_value=book):
            orderbook = adapter.get_orderbook("token-yes")

        self.assertEqual([level.price for level in orderbook.bids], [0.50, 0.40])
        self.assertEqual([level.price for level in orderbook.asks], [0.62, 0.70])

    def test_authenticated_clob_trade_history_is_normalized(self) -> None:
        adapter = PolymarketAdapter(
            {
                "polymarket_l2_headers": {
                    "POLY_ADDRESS": "0x" + "a" * 40,
                    "POLY_API_KEY": "api-key",
                    "POLY_PASSPHRASE": "passphrase",
                    "POLY_SIGNATURE": "signature",
                    "POLY_TIMESTAMP": "1760000000",
                }
            }
        )
        trades = load_fixture("clob_trades.json")
        with patch("market_adapters.polymarket.clob_auth.get_trades", return_value=trades) as get_trades:
            result = adapter.list_trades("token-yes", limit=2, after=1760000000, before=1760000300)

        get_trades.assert_called_once()
        headers, = get_trades.call_args.args
        self.assertEqual(headers["POLY_ADDRESS"], "0x" + "a" * 40)
        self.assertEqual(get_trades.call_args.kwargs["asset_id"], "token-yes")
        self.assertEqual([item.trade_id for item in result], ["clob-trade-1", "clob-trade-2"])
        self.assertEqual([item.side for item in result], ["BUY", "SELL"])
        self.assertAlmostEqual(result[0].size, 12.5)
        self.assertAlmostEqual(result[1].price, 0.47)

    def test_authenticated_clob_trade_history_fails_closed_without_headers(self) -> None:
        adapter = PolymarketAdapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.list_trades("token-yes")

        self.assertIn("explicit L2 headers", str(ctx.exception))

    def test_public_clob_price_history_is_normalized_to_flat_candles(self) -> None:
        adapter = PolymarketAdapter()
        history = load_fixture("price_history.json")

        with patch("market_adapters.polymarket.clob_rest.get_price_history", return_value=history) as get_history:
            result = adapter.list_candles(
                "token-yes",
                resolution="1h",
                from_timestamp=1760000000,
                to_timestamp=1760000300,
            )

        get_history.assert_called_once_with(
            "token-yes",
            start_ts=1760000000,
            end_ts=1760000300,
            interval="1h",
        )
        self.assertEqual([candle.timestamp for candle in result], [1760000100.0, 1760000200.0])
        self.assertEqual([candle.close for candle in result], [0.45, 0.47])
        self.assertTrue(all(candle.volume is None for candle in result))
        self.assertEqual(result[0].raw["t"], 1760000100)

    def test_public_clob_price_history_validates_interval_and_range(self) -> None:
        adapter = PolymarketAdapter()

        with self.assertRaises(MarketConfigurationError) as interval_error:
            adapter.list_candles("token-yes", resolution="5m")
        self.assertIn("price-history interval", str(interval_error.exception))

        with self.assertRaises(MarketConfigurationError) as range_error:
            adapter.list_candles(
                "token-yes",
                from_timestamp=1760000300,
                to_timestamp=1760000000,
            )
        self.assertIn("to_timestamp greater than from_timestamp", str(range_error.exception))

    def test_get_price_falls_back_to_book_midpoint_when_midpoint_payload_is_bad(self) -> None:
        adapter = PolymarketAdapter()
        book = {
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "8"}],
        }
        with patch("market_adapters.polymarket.clob_rest.get_book", return_value=book), patch(
            "market_adapters.polymarket.clob_rest.get_midpoint", side_effect=RuntimeError("bad midpoint")
        ), patch(
            "market_adapters.polymarket.clob_rest.get_last_trade_price", side_effect=RuntimeError("bad last")
        ):
            price = adapter.get_price("token-yes")

        self.assertEqual(price.midpoint, 0.50)
        self.assertIsNone(price.last)

    def test_list_events_skips_malformed_search_items_and_clamps_limit(self) -> None:
        adapter = PolymarketAdapter()
        payload = {
            "events": [
                {"id": "event-1", "title": "Event 1", "active": True},
                "bad",
                {"slug": "event-2", "question": "Event 2", "closed": True},
            ],
            "markets": "not-a-list",
        }
        with patch("market_adapters.polymarket.gamma.public_search", return_value=payload) as search:
            events = adapter.list_events(" election ", limit=250)

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["limit_per_type"], 100)
        self.assertEqual([event.event_id for event in events], ["event-1", "event-2"])
        self.assertEqual(events[0].status, "active")
        self.assertEqual(events[1].status, "closed")

    def test_list_contracts_skips_malformed_event_markets(self) -> None:
        adapter = PolymarketAdapter()
        market = load_fixture("market.json")
        event = {"id": "event-1", "markets": ["bad", market, None]}
        with patch("market_adapters.polymarket.gamma.get_event_by_slug", return_value=event):
            contracts = adapter.list_contracts("event-slug")

        self.assertEqual([c.contract_id for c in contracts], ["token-yes", "token-no"])

    def test_copy_trade_from_activity_uses_paper_order_path(self) -> None:
        adapter = PolymarketAdapter()
        activity = load_fixture("activity_buy.json")

        result = adapter.copy_trade_from_activity(activity)

        self.assertTrue(result.accepted)
        self.assertEqual(result.contract_id, "token-1234567890")
        self.assertIn("BUY", result.message)
        self.assertIn("0.4500", result.message)

    def test_copy_trade_from_activity_rejects_bad_numeric_activity(self) -> None:
        adapter = PolymarketAdapter()
        activity = load_fixture("activity_buy.json")
        activity["size"] = "not-a-size"

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.copy_trade_from_activity(activity)

        self.assertIn("size must be numeric", str(ctx.exception))

    def test_paper_order_is_dry_run_and_does_not_fill(self) -> None:
        adapter = PolymarketAdapter()
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="polymarket",
                contract_id="token-yes",
                side="BUY",
                size=5.0,
                limit_price=0.55,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.filled_size, 0.0)
        self.assertIn("DRY RUN", result.message)

    def test_live_order_is_disabled_by_adapter_config_by_default(self) -> None:
        adapter = PolymarketAdapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.place_live_order(
                PaperOrderRequest(
                    market_id="polymarket",
                    contract_id="token-yes",
                    side="BUY",
                    size=1.0,
                    limit_price=0.5,
                )
            )

        self.assertIn("disabled", str(ctx.exception).lower())

    def test_live_order_requires_limit_before_geoblock_or_credentials(self) -> None:
        adapter = PolymarketAdapter(
            {"live_trading_enabled": True, "live_trading_confirmed": True, "private_key": "not-used"}
        )

        with patch.object(adapter, "check_geoblock") as check_geoblock:
            with self.assertRaises(MarketConfigurationError) as ctx:
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="polymarket",
                        contract_id="token-yes",
                        side="BUY",
                        size=1.0,
                        limit_price=None,
                    )
                )

        self.assertIn("requires a limit price", str(ctx.exception))
        check_geoblock.assert_not_called()

    def test_live_order_rejects_bad_signature_type_with_clear_error(self) -> None:
        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "private_key": "not-a-real-key",
                "signature_type": "bad",
            }
        )

        with patch.object(adapter, "check_geoblock", return_value={"blocked": False}):
            with self.assertRaises(MarketConfigurationError) as ctx:
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="polymarket",
                        contract_id="token-yes",
                        side="BUY",
                        size=1.0,
                        limit_price=0.5,
                    )
                )

        self.assertIn("SIGNATURE_TYPE must be an integer", str(ctx.exception))

    def test_health_check_exposes_runtime_without_secret_values(self) -> None:
        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "private_key": "super-secret",
                "http_timeout_seconds": 4,
            }
        )

        health = adapter.health_check()

        self.assertTrue(health["live_trading_enabled"])
        self.assertEqual(health["runtime"]["timeout_seconds"], 4.0)
        self.assertEqual(health["credential_sources"], [{"name": "PRIVATE_KEY", "source": "config:private_key"}])
        self.assertNotIn("super-secret", str(health))

    def test_health_check_reports_redacted_clob_auth_readiness(self) -> None:
        private_key = "0x" + "1" * 64
        adapter = PolymarketAdapter(
            {
                "private_key": private_key,
                "signature_type": 3,
                "funder_address": "0x" + "2" * 40,
            }
        )

        health = adapter.health_check()
        readiness = health["clob_auth_readiness"]

        self.assertTrue(readiness["ok"])
        self.assertTrue(readiness["sdk_trading_ready"])
        self.assertEqual(readiness["signature_type"]["name"], "POLY_1271")
        self.assertEqual(readiness["private_key"]["redacted"], "***")
        self.assertIn("0x2222", readiness["funder_address"]["redacted"])
        self.assertNotIn(private_key, str(health))

    def test_live_order_readiness_blocks_poly_1271_without_funder_before_client_init(self) -> None:
        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "private_key": "0x" + "1" * 64,
                "signature_type": 3,
            }
        )

        with patch.object(adapter, "check_geoblock", return_value={"blocked": False}), patch(
            "market_adapters.polymarket.PolymarketTrader"
        ) as trader:
            with self.assertRaises(MarketConfigurationError) as ctx:
                adapter.place_live_order(
                    PaperOrderRequest(
                        market_id="polymarket",
                        contract_id="token-yes",
                        side="BUY",
                        size=1.0,
                        limit_price=0.5,
                    )
                )

        self.assertIn("requires an explicit funder", str(ctx.exception))
        trader.assert_not_called()

    def test_order_validation_rejects_bad_price(self) -> None:
        adapter = PolymarketAdapter()

        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="polymarket",
                    contract_id="token-yes",
                    side="BUY",
                    size=1.0,
                    limit_price=1.5,
                )
            )

    def test_health_check_exposes_authenticated_account_and_order_operations(self) -> None:
        health = PolymarketAdapter().health_check()

        self.assertEqual(health["account_recovery_operations"], ["active_orders", "order_detail", "fills"])
        self.assertEqual(
            health["order_management_operations"],
            ["cancel_order", "cancel_orders", "cancel_all_orders", "cancel_market_orders"],
        )
        self.assertFalse(health["order_management_enabled"])

    def test_authenticated_account_recovery_routes_documented_clob_reads(self) -> None:
        adapter = PolymarketAdapter(
            {
                "polymarket_l2_headers": {
                    "POLY_ADDRESS": "0x" + "a" * 40,
                    "POLY_API_KEY": "api-key",
                    "POLY_PASSPHRASE": "passphrase",
                    "POLY_SIGNATURE": "signature",
                    "POLY_TIMESTAMP": "1760000000",
                }
            }
        )
        orders = load_fixture("clob_orders.json")
        order = load_fixture("clob_order.json")
        fills = load_fixture("clob_trades.json")
        order_id = "0x" + "a" * 64
        with patch("market_adapters.polymarket.clob_auth.get_orders", return_value=orders) as get_orders, patch(
            "market_adapters.polymarket.clob_auth.get_order", return_value=order
        ) as get_order, patch("market_adapters.polymarket.clob_auth.get_trades", return_value=fills) as get_trades:
            active = adapter.account_recovery(
                "active_orders", market_id="0x" + "b" * 64, contract_id="1234567890", next_cursor="MTAw"
            )
            detail = adapter.account_recovery("order_detail", order_id=order_id)
            recovered_fills = adapter.account_recovery(
                "fills", contract_id="1234567890", after=1760000000, before=1760000300, limit=25
            )

        self.assertEqual(active, orders)
        self.assertEqual(detail, order)
        self.assertEqual(recovered_fills, fills)
        self.assertEqual(get_orders.call_args.kwargs["market"], "0x" + "b" * 64)
        self.assertEqual(get_orders.call_args.kwargs["asset_id"], "1234567890")
        self.assertEqual(get_order.call_args.args[0], order_id)
        self.assertEqual(get_trades.call_args.kwargs["asset_id"], "1234567890")
        self.assertEqual(get_trades.call_args.kwargs["after"], 1760000000)
        self.assertEqual(get_trades.call_args.kwargs["limit"], 25)

    def test_polymarket_order_management_requires_opt_in_and_exact_confirmation(self) -> None:
        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "polymarket_l2_headers": {
                    "POLY_ADDRESS": "0x" + "a" * 40,
                    "POLY_API_KEY": "api-key",
                    "POLY_PASSPHRASE": "passphrase",
                    "POLY_SIGNATURE": "signature",
                    "POLY_TIMESTAMP": "1760000000",
                },
            }
        )
        with self.assertRaises(MarketConfigurationError) as disabled:
            adapter.manage_orders("cancel_all_orders", confirm_order_management="I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS")
        self.assertIn("disabled", str(disabled.exception).lower())

        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "polymarket_order_management_enabled": True,
                "polymarket_l2_headers": {
                    "POLY_ADDRESS": "0x" + "a" * 40,
                    "POLY_API_KEY": "api-key",
                    "POLY_PASSPHRASE": "passphrase",
                    "POLY_SIGNATURE": "signature",
                    "POLY_TIMESTAMP": "1760000000",
                },
            }
        )
        with self.assertRaises(MarketConfigurationError) as confirmation:
            adapter.manage_orders("cancel_all_orders", confirm_order_management="no")
        self.assertIn("exact confirmation", str(confirmation.exception).lower())

    def test_polymarket_order_management_routes_fixed_cancel_endpoints(self) -> None:
        adapter = PolymarketAdapter(
            {
                "live_trading_enabled": True,
                "live_trading_confirmed": True,
                "polymarket_order_management_enabled": True,
                "polymarket_l2_headers": {
                    "POLY_ADDRESS": "0x" + "a" * 40,
                    "POLY_API_KEY": "api-key",
                    "POLY_PASSPHRASE": "passphrase",
                    "POLY_SIGNATURE": "signature",
                    "POLY_TIMESTAMP": "1760000000",
                },
            }
        )
        order_id = "0x" + "a" * 64
        second_id = "0x" + "c" * 64
        confirmation = "I_UNDERSTAND_THIS_CHANGES_LIVE_ORDERS"
        with patch("market_adapters.polymarket.clob_auth.cancel_order", return_value=load_fixture("cancel_order_response.json")) as cancel_order, patch(
            "market_adapters.polymarket.clob_auth.cancel_orders", return_value=load_fixture("cancel_orders_response.json")
        ) as cancel_orders, patch(
            "market_adapters.polymarket.clob_auth.cancel_all_orders", return_value=load_fixture("cancel_all_response.json")
        ) as cancel_all, patch(
            "market_adapters.polymarket.clob_auth.cancel_market_orders", return_value=load_fixture("cancel_market_response.json")
        ) as cancel_market:
            single = adapter.manage_orders("cancel_order", order_id=order_id, confirm_order_management=confirmation)
            batch = adapter.manage_orders("cancel_orders", orders=[order_id, second_id, order_id], confirm_order_management=confirmation)
            global_cancel = adapter.manage_orders("cancel_all_orders", confirm_order_management=confirmation)
            market_cancel = adapter.manage_orders(
                "cancel_market_orders",
                market_id="0x" + "b" * 64,
                asset_id="1234567890",
                confirm_order_management=confirmation,
            )

        self.assertTrue(all(result["live"] for result in (single, batch, global_cancel, market_cancel)))
        cancel_order.assert_called_once_with(order_id, cancel_order.call_args.args[1])
        self.assertEqual(cancel_orders.call_args.args[0], [order_id, second_id])
        cancel_all.assert_called_once()
        self.assertEqual(cancel_market.call_args.args[:2], ("0x" + "b" * 64, "1234567890"))


if __name__ == "__main__":
    unittest.main()
