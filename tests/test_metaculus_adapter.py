from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from market_adapters import MetaculusAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "metaculus"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class MetaculusAdapterTests(unittest.TestCase):
    def make_adapter(self) -> MetaculusAdapter:
        adapter = MetaculusAdapter()
        posts = load_fixture("posts")
        post_binary = load_fixture("post_binary")
        post_multiple = load_fixture("post_multiple")
        post_numeric = load_fixture("post_numeric")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers["Authorization"], "Token unit-test-token")
            if url.endswith("/posts/"):
                return posts
            if url.endswith("/posts/1001/"):
                return post_binary
            if url.endswith("/posts/1002/"):
                return post_multiple
            if url.endswith("/posts/1003/"):
                return post_numeric
            raise AssertionError(f"unexpected Metaculus URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_metadata_advertises_forecast_submission_capabilities(self) -> None:
        adapter = MetaculusAdapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "metaculus")
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertTrue(health["trading_supported"])
        self.assertTrue(health["forecast_submission_supported"])
        self.assertEqual(health["trading_semantics"], "forecast_submission_not_exchange_execution")
        self.assertEqual(health["account_recovery_operations"], ["forecast_posts"])
        self.assertEqual(health["authenticated_account_endpoints"], ["/posts/?forecaster_id=..."])
        self.assertIn("metaculus.com/api", health["api_base_url"])

    def test_missing_api_token_is_clear(self) -> None:
        adapter = MetaculusAdapter()

        with self.assertRaises(MarketConfigurationError) as ctx:
            adapter.list_events("demo")

        self.assertIn("METACULUS_API_TOKEN", str(ctx.exception))

    def test_list_events_reads_authenticated_posts_feed(self) -> None:
        adapter = self.make_adapter()

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            events = adapter.list_events("demo", limit=10)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].market_id, "metaculus")
        self.assertEqual(events[0].event_id, "1001")
        self.assertEqual(events[0].status, "open")
        self.assertIn("demo launch", events[0].title)

    def test_account_recovery_reads_documented_forecaster_posts(self) -> None:
        adapter = MetaculusAdapter()
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, params, headers))
            self.assertEqual(headers["Authorization"], "Token unit-test-token")
            return load_fixture("forecast_posts")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            response = adapter.account_recovery(
                "forecast_posts",
                forecaster_id="123",
                limit=10,
                offset=20,
                with_cp=True,
                include_cp_history=True,
                include_descriptions=True,
            )

        self.assertEqual(response["results"][0]["id"], 1101)
        self.assertEqual(calls[0][0], "https://www.metaculus.com/api/posts/")
        self.assertEqual(
            calls[0][1],
            {
                "forecaster_id": 123,
                "limit": 10,
                "offset": 20,
                "with_cp": True,
                "include_cp_history": True,
                "include_descriptions": True,
            },
        )

    def test_account_recovery_requires_bounded_forecaster_identity(self) -> None:
        adapter = MetaculusAdapter()
        for invalid in (None, "", "0", "1.5", "../outside", True):
            with self.subTest(invalid=invalid):
                with self.assertRaises(MarketConfigurationError):
                    adapter.account_recovery("forecast_posts", forecaster_id=invalid)

        with self.assertRaises(MarketConfigurationError):
            adapter.account_recovery("unsupported", forecaster_id=123)

        configured = MetaculusAdapter({"metaculus_forecaster_id": 321})
        configured.runtime.get_json = lambda url, *, params=None, headers=None: load_fixture("posts")  # type: ignore[method-assign]
        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            configured.account_recovery("forecast_posts")

    def test_list_contracts_maps_binary_multiple_choice_and_numeric_questions(self) -> None:
        adapter = self.make_adapter()

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            binary = adapter.list_contracts("1001")
            multiple = adapter.list_contracts("1002")
            numeric = adapter.list_contracts("1003")

        self.assertEqual([contract.contract_id for contract in binary], ["1001:501:YES", "1001:501:NO"])
        self.assertEqual(
            [contract.contract_id for contract in multiple],
            ["1002:601:CHOICE:alpha", "1002:601:CHOICE:beta"],
        )
        self.assertEqual([contract.contract_id for contract in numeric], ["1003:701:VALUE"])

    def test_get_price_supports_binary_yes_no_choice_and_numeric_values(self) -> None:
        adapter = self.make_adapter()

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            yes = adapter.get_price("1001:501:YES")
            no = adapter.get_price("1001:501:NO")
            choice = adapter.get_price("1002:601:CHOICE:beta")
            value = adapter.get_price("1003:701:VALUE")

        self.assertEqual(yes.last, 0.64)
        self.assertAlmostEqual(no.last or 0, 0.36)
        self.assertEqual(choice.last, 0.75)
        self.assertEqual(value.last, 1250)
        self.assertEqual(yes.source, "metaculus_api")

    def test_list_candles_maps_official_forecast_history_for_all_question_types(self) -> None:
        adapter = self.make_adapter()

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            binary_yes = adapter.list_candles("1001:501:YES", resolution="forecast")
            binary_no = adapter.list_candles("1001:501:NO", from_timestamp=1704153600)
            multiple_beta = adapter.list_candles("1002:601:CHOICE:beta")
            numeric = adapter.list_candles("1003:701:VALUE", to_timestamp=1704153600)

        self.assertEqual([c.close for c in binary_yes], [0.4, 0.64])
        self.assertEqual([c.close for c in binary_no], [0.36])
        self.assertEqual([c.close for c in multiple_beta], [0.4, 0.75])
        self.assertEqual([c.close for c in numeric], [1000.0, 1250.0])
        self.assertEqual([c.open for c in binary_yes], [c.close for c in binary_yes])
        self.assertTrue(all(c.volume is None for c in binary_yes))
        self.assertEqual(binary_yes[0].raw["aggregation_method"], "recency_weighted")

    def test_list_candles_rejects_unsupported_resampling_and_inaccessible_history(self) -> None:
        adapter = self.make_adapter()
        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            with self.assertRaises(MarketConfigurationError):
                adapter.list_candles("1001:501:YES", resolution="5m")

        no_history = MetaculusAdapter()
        no_history.runtime.get_json = lambda url, *, params=None, headers=None: {
            "id": 9001,
            "question": {"id": 901, "type": "binary", "aggregations": {}},
        }  # type: ignore[method-assign]
        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            with self.assertRaises(MarketConfigurationError) as ctx:
                no_history.list_candles("9001:901:YES")
        self.assertIn("history", str(ctx.exception))

    def test_unavailable_community_prediction_is_clear(self) -> None:
        adapter = MetaculusAdapter()
        post = {
            "id": 2001,
            "title": "Private forecast",
            "question": {
                "id": 801,
                "title": "Private forecast",
                "type": "binary",
            },
        }

        def fake_get_json(url: str, *, params=None, headers=None):
            return post

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            with self.assertRaises(MarketConfigurationError) as ctx:
                adapter.get_price("2001:801:YES")

        self.assertIn("Community Prediction", str(ctx.exception))

    def test_orderbook_remains_unsupported(self) -> None:
        adapter = self.make_adapter()

        with self.assertRaises(UnsupportedFeatureError) as orderbook_ctx:
            adapter.get_orderbook("1001:501:YES")
        self.assertEqual(orderbook_ctx.exception.feature, "orderbook_reading")

    def test_paper_forecast_preview_builds_official_payloads_for_all_question_types(self) -> None:
        adapter = self.make_adapter()

        binary = adapter.place_paper_order(
            PaperOrderRequest("metaculus", "1001:501:YES", "BUY", 1, 0.7)
        )
        self.assertTrue(binary.accepted)
        self.assertEqual(binary.raw["endpoint"], "/questions/forecast/")
        self.assertEqual(binary.raw["request"][0]["question"], 501)
        self.assertEqual(binary.raw["request"][0]["source"], "api")
        self.assertEqual(binary.raw["request"][0]["probability_yes"], 0.7)
        self.assertEqual(binary.raw["semantics"], "forecast_submission")

        no = adapter.place_paper_order(
            PaperOrderRequest("metaculus", "1001:501:NO", "BUY", 1, 0.25)
        )
        self.assertEqual(no.raw["request"][0]["probability_yes"], 0.75)

        multiple = adapter.place_paper_order(
            PaperOrderRequest(
                "metaculus",
                "1002:601:CHOICE:beta",
                "BUY",
                1,
                metadata={"probability_yes_per_category": {"Alpha": 0.4, "Beta": 0.6}},
            )
        )
        self.assertEqual(
            multiple.raw["request"][0]["probability_yes_per_category"],
            {"Alpha": 0.4, "Beta": 0.6},
        )

        cdf = [index / 200 for index in range(201)]
        numeric = adapter.place_paper_order(
            PaperOrderRequest(
                "metaculus",
                "1003:701:VALUE",
                "BUY",
                1,
                metadata={"continuous_cdf": cdf},
            )
        )
        self.assertEqual(numeric.raw["request"][0]["continuous_cdf"], cdf)

    def test_forecast_submission_validation_is_fail_closed(self) -> None:
        adapter = self.make_adapter()

        invalid_orders = [
            PaperOrderRequest("metaculus", "1001:501:YES", "SELL", 1, 0.5),
            PaperOrderRequest("metaculus", "1001:501:YES", "BUY", 1),
            PaperOrderRequest(
                "metaculus",
                "1002:601:CHOICE:beta",
                "BUY",
                1,
                metadata={"probability_yes_per_category": {"Alpha": 0.2, "Beta": 0.2}},
            ),
            PaperOrderRequest(
                "metaculus",
                "1003:701:VALUE",
                "BUY",
                1,
                metadata={"continuous_cdf": [0.5] * 200 + [0.4]},
            ),
        ]
        for order in invalid_orders:
            with self.subTest(order=order):
                with self.assertRaises(MarketConfigurationError):
                    adapter.place_paper_order(order)

    def test_live_forecast_submission_uses_shared_safety_gates_and_official_route(self) -> None:
        adapter = MetaculusAdapter(
            {"live_trading_enabled": True, "live_trading_confirmed": True, "live_trading_max_size": 2}
        )
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return {"status": "accepted", "id": "forecast-1"}

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            response = adapter.place_live_order(
                PaperOrderRequest("metaculus", "1001:501:YES", "BUY", 1, 0.7)
            )

        self.assertTrue(response["live"])
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/api/questions/forecast/"))
        self.assertEqual(calls[0][2][0]["question"], 501)
        self.assertEqual(calls[0][2][0]["probability_yes"], 0.7)
        self.assertEqual(calls[0][3]["Authorization"], "Token unit-test-token")

        with patch.dict("os.environ", {"METACULUS_API_TOKEN": "unit-test-token"}):
            zero_response = adapter.place_live_order(
                PaperOrderRequest("metaculus", "1001:501:YES", "BUY", 1, 0.0)
            )
        self.assertTrue(zero_response["live"])
        self.assertEqual(calls[1][2][0]["probability_yes"], 0.0)


if __name__ == "__main__":
    unittest.main()
