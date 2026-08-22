from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import GoodJudgmentOpenAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "good_judgment_open"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class GoodJudgmentOpenAdapterTests(unittest.TestCase):
    def make_adapter(self, **config) -> GoodJudgmentOpenAdapter:
        settings = {"good_judgment_open_api_token": "fixture-token", **config}
        adapter = GoodJudgmentOpenAdapter(settings)
        questions = load_fixture("questions")
        history = load_fixture("prediction_sets")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers.get("Authorization"), "Bearer fixture-token")
            if url.endswith("/api/v1/questions"):
                if params and params.get("ids") == "1201":
                    return {"questions": [questions["questions"][0]]}
                return questions
            if url.endswith("/api/v1/prediction_sets"):
                return history
            raise AssertionError(f"unexpected Good Judgment Open URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter

    def test_metadata_and_health_publish_guarded_forecast_capabilities(self) -> None:
        adapter = self.make_adapter()
        health = adapter.health_check()

        self.assertTrue(health["ok"])
        self.assertEqual(adapter.market_id, "good_judgment_open")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.candle_history)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertTrue(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertEqual(health["authentication_mode"], "bearer_token")
        self.assertEqual(health["api_base_url"], "https://www.gjopen.com")

    def test_list_events_and_contracts_use_documented_questions_feed(self) -> None:
        adapter = self.make_adapter()

        events = adapter.list_events("demo", limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "1201")
        self.assertEqual(events[0].status, "open")
        self.assertIn("demo launch", events[0].title)

        contracts = adapter.list_contracts("1201")
        self.assertEqual([contract.contract_id for contract in contracts], ["1201:7", "1201:8"])
        self.assertEqual(contracts[0].outcome, "Yes")

    def test_price_and_forecast_history_are_normalized_without_synthetic_volume(self) -> None:
        adapter = self.make_adapter()

        snapshot = adapter.get_price("1201:7")
        self.assertEqual(snapshot.last, 0.64)
        self.assertEqual(snapshot.source, "cultivate_forecasts_api")

        candles = adapter.list_candles("1201:7", resolution="forecast")
        self.assertEqual([c.close for c in candles], [0.4, 0.64])
        self.assertEqual([c.open for c in candles], [c.close for c in candles])
        self.assertTrue(all(c.volume is None for c in candles))
        self.assertEqual(candles[0].raw["prediction_set"]["id"], 301)

        with self.assertRaises(MarketConfigurationError):
            adapter.list_candles("1201:7", resolution="5m")

    def test_paper_forecast_preview_is_local_and_orderbook_is_explicitly_unsupported(self) -> None:
        adapter = self.make_adapter()
        result = adapter.place_paper_order(
            PaperOrderRequest(
                market_id="good_judgment_open",
                contract_id="1201:7",
                side="BUY",
                size=1,
                limit_price=0.7,
                metadata={"rationale": "fixture rationale"},
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.average_price, 0.7)
        self.assertEqual(result.raw["request"]["prediction_set"]["predictions_attributes"][0]["answer_id"], 7)
        self.assertEqual(result.raw["request"]["prediction_set"]["rationale"], "fixture rationale")

        with self.assertRaises(UnsupportedFeatureError) as ctx:
            adapter.get_orderbook("1201:7")
        self.assertEqual(ctx.exception.feature, "orderbook_reading")

    def test_live_submission_requires_shared_safety_gates_and_posts_documented_shape(self) -> None:
        adapter = self.make_adapter(
            live_trading_enabled=True,
            live_trading_confirmed=True,
            live_trading_max_notional=1.0,
        )
        calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            calls.append((method, url, json_body, headers))
            return load_fixture("prediction_submission")

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        response = adapter.place_live_order(
            PaperOrderRequest(
                market_id="good_judgment_open",
                contract_id="1201:7",
                side="BUY",
                size=1,
                limit_price=0.7,
            )
        )

        self.assertTrue(response["live"])
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/api/v1/questions/1201/prediction_sets"))
        self.assertEqual(calls[0][2]["prediction_set"]["predictions_attributes"][0]["answer_id"], 7)
        self.assertEqual(calls[0][2]["prediction_set"]["predictions_attributes"][0]["forecasted_probability"], 0.7)
        self.assertEqual(calls[0][3]["Authorization"], "Bearer fixture-token")

    def test_oauth_password_flow_is_cached_and_never_exposed_in_health(self) -> None:
        adapter = GoodJudgmentOpenAdapter(
            {
                "good_judgment_open_email": "forecaster@example.test",
                "good_judgment_open_password": "not-a-real-password",
            }
        )
        questions = load_fixture("questions")
        oauth_calls = []

        def fake_request_json(method: str, url: str, *, params=None, json_body=None, headers=None):
            oauth_calls.append((method, url, json_body, headers))
            if url.endswith("/oauth/token"):
                return load_fixture("oauth_token")
            raise AssertionError(f"unexpected OAuth URL: {url}")

        def fake_get_json(url: str, *, params=None, headers=None):
            self.assertEqual(headers.get("Authorization"), "Bearer fixture-access-token")
            return questions

        adapter.runtime.request_json = fake_request_json  # type: ignore[method-assign]
        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        events = adapter.list_events("demo")

        self.assertEqual(len(events), 1)
        self.assertEqual(len(oauth_calls), 1)
        self.assertEqual(oauth_calls[0][2]["grant_type"], "password")
        self.assertNotIn("not-a-real-password", str(adapter.health_check()))

    def test_invalid_contract_and_probability_are_rejected(self) -> None:
        adapter = self.make_adapter()
        with self.assertRaises(MarketConfigurationError):
            adapter.get_price("1201:bad/id")
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="good_judgment_open",
                    contract_id="1201:7",
                    side="SELL",
                    size=1,
                    limit_price=0.5,
                )
            )
        with self.assertRaises(MarketConfigurationError):
            adapter.place_paper_order(
                PaperOrderRequest(
                    market_id="good_judgment_open",
                    contract_id="1201:7",
                    side="BUY",
                    size=1,
                    limit_price=1.5,
                )
            )


if __name__ == "__main__":
    unittest.main()
