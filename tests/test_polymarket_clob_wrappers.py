from __future__ import annotations

import unittest
from unittest.mock import patch

from polymarket import clob_auth, clob_rest


L2_HEADERS = {
    "POLY_ADDRESS": "0x" + "a" * 40,
    "POLY_API_KEY": "key",
    "POLY_PASSPHRASE": "passphrase",
    "POLY_SIGNATURE": "signature",
    "POLY_TIMESTAMP": "123",
}


class ClobAuthWrapperTests(unittest.TestCase):
    def test_l2_headers_normalize_values_and_reject_missing_required_fields(self) -> None:
        headers = {**L2_HEADERS, "EXTRA": 7, "EMPTY": ""}
        self.assertEqual(clob_auth._l2_headers(headers)["EXTRA"], "7")
        self.assertNotIn("EMPTY", clob_auth._l2_headers(headers))

        for header in clob_auth.REQUIRED_L2_HEADERS:
            incomplete = dict(L2_HEADERS)
            incomplete.pop(header)
            with self.subTest(header=header), self.assertRaisesRegex(ValueError, header):
                clob_auth._l2_headers(incomplete)

    def test_authenticated_reads_remain_available_and_mutations_fail_closed(self) -> None:
        with patch("polymarket.clob_auth._request_json", return_value={"orderID": "order-1"}) as request:
            self.assertEqual(
                clob_auth.get_orders(L2_HEADERS, order_id="order-1", market="market-1", asset_id="asset-1", next_cursor="next"),
                {"orderID": "order-1"},
            )
            self.assertEqual(clob_auth.get_trades(L2_HEADERS, market="market-1"), {"orderID": "order-1"})
            self.assertEqual(clob_auth.get_order_scoring_status("order-1", L2_HEADERS), {"orderID": "order-1"})
            self.assertEqual(clob_auth.get_user_rewards(L2_HEADERS, market="market-1"), {"orderID": "order-1"})
            self.assertEqual(clob_auth.get_user_reward_total(L2_HEADERS, market="market-1"), {"orderID": "order-1"})
            self.assertEqual(clob_auth.get_user_reward_percentages(L2_HEADERS, market="market-1"), {"orderID": "order-1"})
            self.assertEqual(clob_auth.get_user_reward_markets(L2_HEADERS, market="market-1"), {"orderID": "order-1"})

            for mutation in (
                lambda: clob_auth.post_order({"price": "0.5"}, L2_HEADERS),
                lambda: clob_auth.post_orders([{"id": 1}], L2_HEADERS),
                lambda: clob_auth.cancel_order("order-1", L2_HEADERS),
                lambda: clob_auth.cancel_orders(["one"], L2_HEADERS),
                lambda: clob_auth.cancel_all_orders(L2_HEADERS),
                lambda: clob_auth.cancel_market_orders("market-1", "asset-1", L2_HEADERS),
                lambda: clob_auth.send_heartbeat(L2_HEADERS),
            ):
                with self.assertRaisesRegex(RuntimeError, "CLOB V2"):
                    mutation()

        names = [call.args[0] for call in request.call_args_list]
        self.assertEqual(
            names,
            [
                "get_orders",
                "trades",
                "order_scoring",
                "user_rewards",
                "user_reward_total",
                "user_reward_percentages",
                "user_reward_markets",
            ],
        )
        self.assertEqual(request.call_args_list[0].kwargs["params"], {"id": "order-1", "market": "market-1", "asset_id": "asset-1", "next_cursor": "next"})

    def test_get_order_uses_order_path_and_non_object_responses_are_empty(self) -> None:
        with patch("polymarket.clob_auth.request_json", return_value=["unexpected"]) as request:
            self.assertEqual(clob_auth.get_order("order-1", L2_HEADERS, timeout=4), {})

        self.assertEqual(request.call_args.args[0].path, "/order/{order_id}")
        self.assertEqual(request.call_args.kwargs["path"], "/order/order-1")
        self.assertEqual(request.call_args.kwargs["timeout"], 4)


class ClobRestWrapperTests(unittest.TestCase):
    def test_public_read_wrappers_normalize_documented_payload_shapes(self) -> None:
        with (
            patch("polymarket.clob_rest._get_json", return_value={"value": 1}) as get_json,
            patch("polymarket.clob_rest._post_json", return_value={"value": 1}) as post_json,
        ):
            self.assertEqual(clob_rest.get_book("token"), {"value": 1})
            self.assertEqual(clob_rest.get_midpoints(["one", "two"]), {"value": 1})
            self.assertEqual(clob_rest.get_midpoints_body(["one", "two"]), {"value": 1})
            self.assertEqual(clob_rest.get_prices(["one"], ["buy"]), {"value": 1})
            self.assertEqual(clob_rest.get_prices_body([{ "token_id": "one", "side": "buy" }, {"side": "sell"}]), {"value": 1})
            self.assertEqual(clob_rest.get_spreads(["one"]), {"value": 1})
            self.assertEqual(clob_rest.get_batch_price_history(["market"], interval="1h"), {"value": 1})
            self.assertEqual(clob_rest.get_fee_rate("token"), {"value": 1})
            self.assertEqual(clob_rest.get_fee_rate_by_token("token"), {"value": 1})
            self.assertEqual(clob_rest.get_tick_size("token"), {"value": 1})
            self.assertEqual(clob_rest.get_tick_size_by_token("token"), {"value": 1})
            self.assertEqual(clob_rest.get_clob_market_info("condition"), {"value": 1})
            self.assertEqual(clob_rest.get_market_by_token("token"), {"value": 1})
            self.assertEqual(clob_rest.get_server_time(), {"value": 1})
            self.assertEqual(clob_rest.list_simplified_markets("next"), {"value": 1})
            self.assertEqual(clob_rest.list_sampling_markets("next"), {"value": 1})
            self.assertEqual(clob_rest.list_sampling_simplified_markets("next"), {"value": 1})
            self.assertEqual(clob_rest.get_current_rewards_config(sponsored=True, next_cursor="next"), {"value": 1})
            self.assertEqual(clob_rest.get_raw_rewards_for_market("condition"), {"value": 1})
            self.assertEqual(clob_rest.get_rewards_markets("next"), {"value": 1})
            self.assertEqual(clob_rest.get_builder_trades("builder", market="market"), {"value": 1})

        self.assertEqual(post_json.call_args_list[0].args[1], [{"token_id": "one"}, {"token_id": "two"}])
        self.assertEqual(post_json.call_args_list[1].args[1], [{"token_id": "one", "side": "BUY"}])
        calls_by_endpoint = {call.args[0]: call for call in get_json.call_args_list}
        self.assertEqual(calls_by_endpoint["simplified_markets"].kwargs["params"], {"next_cursor": "next"})
        self.assertEqual(
            calls_by_endpoint["rewards_current"].kwargs["params"],
            {"sponsored": "true", "next_cursor": "next"},
        )

    def test_scalar_and_list_payload_wrappers_handle_unexpected_shapes(self) -> None:
        with patch("polymarket.clob_rest._get_json", side_effect=[{"price": "0.51"}, {"spread": "0.02"}, 123, ["unexpected"], ["unexpected"]]):
            self.assertEqual(clob_rest.get_price("token", "buy"), 0.51)
            self.assertEqual(clob_rest.get_spread("token"), 0.02)
            self.assertEqual(clob_rest.get_server_time(), {"time": 123})
            self.assertEqual(clob_rest.get_last_trade_prices(["token"]), ["unexpected"])
            self.assertEqual(clob_rest.get_current_rebated_fees("2026-01-01", "maker"), ["unexpected"])


if __name__ == "__main__":
    unittest.main()
