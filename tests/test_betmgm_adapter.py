from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_adapters import BetMGMAdapter, PaperOrderRequest, UnsupportedFeatureError
from market_adapters.errors import MarketConfigurationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "betmgm"
FIXTURE_ID = "V2:4:123456"
CONTRACT_ID = f"{FIXTURE_ID}|987654|1001"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class BetMGMAdapterTests(unittest.TestCase):
    def make_adapter(self, config=None):
        merged = {
            "betmgm_access_id": "partner-access-id",
            "betmgm_access_id_token": "partner-access-token",
            "min_request_interval_seconds": 0,
            **dict(config or {}),
        }
        adapter = BetMGMAdapter(merged)
        calls = []

        def fake_get_json(url: str, *, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            if url.endswith("/fixtures"):
                return load_fixture("fixtures")
            raise AssertionError(f"unexpected BetMGM URL: {url}")

        adapter.runtime.get_json = fake_get_json  # type: ignore[method-assign]
        return adapter, calls

    def test_metadata_and_health_report_read_only_partner_surface(self) -> None:
        adapter = BetMGMAdapter(
            {
                "betmgm_access_id": "partner-access-id",
                "betmgm_access_id_token": "partner-access-token",
                "min_request_interval_seconds": 0,
            }
        )
        health = adapter.health_check()

        self.assertEqual(adapter.market_id, "betmgm")
        self.assertTrue(adapter.capabilities.market_discovery)
        self.assertTrue(adapter.capabilities.event_listing)
        self.assertTrue(adapter.capabilities.price_reading)
        self.assertTrue(adapter.capabilities.alerts)
        self.assertTrue(adapter.capabilities.paper_trading)
        self.assertFalse(adapter.capabilities.orderbook_reading)
        self.assertFalse(adapter.capabilities.live_trading)
        self.assertFalse(adapter.capabilities.copy_trading)
        self.assertTrue(health["credentials_configured"])
        self.assertTrue(health["partner_access_required"])
        self.assertIn("sportsapi", health["api_base_url"])
        self.assertNotIn("partner-access-id", str(health))
        self.assertNotIn("partner-access-token", str(health))

    def test_list_events_and_contracts_use_documented_fixture_schema(self) -> None:
        adapter, calls = self.make_adapter()

        events = adapter.list_events("arsenal", limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, FIXTURE_ID)
        self.assertEqual(events[0].title, "Arsenal v Chelsea")
        self.assertEqual(events[0].status, "not started")
        self.assertEqual(calls[-1][1]["onlyMainMarkets"], True)
        self.assertEqual(calls[-1][2]["Bwin-AccessId"], "partner-access-id")
        self.assertEqual(calls[-1][2]["Bwin-AccessIdToken"], "partner-access-token")

        contracts = adapter.list_contracts(FIXTURE_ID)
        self.assertEqual(len(contracts), 3)
        self.assertEqual(contracts[0].contract_id, CONTRACT_ID)
        self.assertEqual(contracts[0].outcome, "Arsenal")
        self.assertEqual(calls[-1][1]["fixtureIds"], [FIXTURE_ID])
        self.assertEqual(calls[-1][1]["onlyMainMarkets"], False)

    def test_prices_normalize_documented_odds_to_implied_probability(self) -> None:
        adapter, _ = self.make_adapter()
        price = adapter.get_price(CONTRACT_ID)

        self.assertEqual(price.contract_id, CONTRACT_ID)
        self.assertEqual(price.last, 0.5)
        self.assertIsNone(price.bid)
        self.assertEqual(price.source, "betmgm_sports_api_implied_probability")
        self.assertEqual(BetMGMAdapter._price_probability({"usOdds": -150}), 0.6)
        self.assertEqual(BetMGMAdapter._price_probability({"fraction": {"numerator": 1, "denominator": 1}}), 0.5)

    def test_paper_order_is_local_and_validates_inputs(self) -> None:
        adapter, _ = self.make_adapter()
        result = adapter.place_paper_order(
            PaperOrderRequest("betmgm", CONTRACT_ID, "BUY", 5, 0.5)
        )
        self.assertTrue(result.accepted)
        self.assertIn("DRY RUN", result.message)
        self.assertTrue(result.raw["official_api_is_read_only"])

        for order in (
            PaperOrderRequest("other", CONTRACT_ID, "BUY", 1, 0.5),
            PaperOrderRequest("betmgm", "", "BUY", 1, 0.5),
            PaperOrderRequest("betmgm", CONTRACT_ID, "SELL", 1, 0.5),
            PaperOrderRequest("betmgm", CONTRACT_ID, "BUY", 0, 0.5),
            PaperOrderRequest("betmgm", CONTRACT_ID, "BUY", 1, 1.1),
        ):
            with self.subTest(order=order):
                with self.assertRaises(MarketConfigurationError):
                    adapter.place_paper_order(order)

    def test_unsupported_operations_and_missing_partner_credentials_fail_closed(self) -> None:
        adapter, _ = self.make_adapter()
        with self.assertRaises(UnsupportedFeatureError) as orderbook_ctx:
            adapter.get_orderbook(CONTRACT_ID)
        self.assertEqual(orderbook_ctx.exception.feature, "orderbook_reading")

        with self.assertRaises(UnsupportedFeatureError) as live_ctx:
            adapter.place_live_order(PaperOrderRequest("betmgm", CONTRACT_ID, "BUY", 1, 0.5))
        self.assertEqual(live_ctx.exception.feature, "live_trading")

        with self.assertRaises(UnsupportedFeatureError) as copy_ctx:
            adapter.copy_trade_from_activity({})
        self.assertEqual(copy_ctx.exception.feature, "copy_trading")

        missing = BetMGMAdapter({"min_request_interval_seconds": 0})
        missing.runtime.get_json = lambda *args, **kwargs: {}  # type: ignore[method-assign]
        with self.assertRaises(MarketConfigurationError):
            missing.list_events(limit=1)

        adapter.runtime.get_json = lambda *args, **kwargs: {"items": [{}]}  # type: ignore[method-assign]
        with self.assertRaises(MarketConfigurationError):
            adapter.list_contracts(FIXTURE_ID)

        adapter.runtime.get_json = lambda *args, **kwargs: {"items": []}  # type: ignore[method-assign]
        with self.assertRaises(MarketConfigurationError):
            adapter.get_price(CONTRACT_ID)


if __name__ == "__main__":
    unittest.main()
