from __future__ import annotations

import unittest
from unittest.mock import patch

from market_adapters import (
    MARKET_CATALOG,
    BetfairExchangeAdapter,
    LimitlessAdapter,
    ManifoldAdapter,
    MatchbookAdapter,
    OpinionAdapter,
    PaperOrderRequest,
    ProphetExchangeAdapter,
    SmarketsAdapter,
    XOMarketAdapter,
    build_default_registry,
)
from market_adapters.errors import MarketConfigurationError


LIVE_CONFIG = {
    "live_trading_enabled": True,
    "live_trading_confirmed": True,
    "live_trading_max_size": 100,
    "live_trading_max_notional": 10,
}


class LiveOrderSafetyTests(unittest.TestCase):
    def test_every_live_adapter_declares_an_exposure_model(self) -> None:
        registry = build_default_registry()
        for metadata in MARKET_CATALOG:
            if not metadata.capabilities.live_trading:
                continue
            with self.subTest(market_id=metadata.market_id):
                model = registry.create(metadata.market_id).live_order_exposure_model
                self.assertIsInstance(model, str)
                self.assertTrue(model.strip())

    def test_lay_venues_cap_odds_based_liability_not_probability_notional(self) -> None:
        cases = (
            (BetfairExchangeAdapter, "betfair_exchange"),
            (MatchbookAdapter, "matchbook"),
        )
        for adapter_type, market_id in cases:
            adapter = adapter_type(LIVE_CONFIG)
            with self.subTest(market_id=market_id, side="BACK"):
                preview = adapter.preflight_live_order(
                    PaperOrderRequest(market_id, "contract-1", "BACK", 2, 0.1)
                )
                self.assertEqual(preview["approx_notional"], 2)
            with self.subTest(market_id=market_id, side="LAY"):
                with self.assertRaisesRegex(MarketConfigurationError, "notional 18 exceeds configured max 10"):
                    adapter.preflight_live_order(
                        PaperOrderRequest(market_id, "contract-1", "LAY", 2, 0.1)
                    )

    def test_limitless_rejects_metadata_wire_identity_and_amount(self) -> None:
        adapter = LimitlessAdapter(LIVE_CONFIG)
        for metadata in ({"maker_amount": 1}, {"token_id": "attacker-token"}):
            with self.subTest(metadata=metadata):
                with self.assertRaisesRegex(MarketConfigurationError, "derived from the reviewed order"):
                    adapter.place_live_order(
                        PaperOrderRequest(
                            "limitless_exchange",
                            "market-one:YES",
                            "BUY",
                            2,
                            None,
                            {"order_type": "FOK", **metadata},
                        )
                    )

    def test_opinion_rejects_metadata_wire_amount_before_sdk_submission(self) -> None:
        adapter = OpinionAdapter(LIVE_CONFIG)
        with patch.object(adapter, "_create_clob_client") as client:
            with self.assertRaisesRegex(MarketConfigurationError, "derived from the preflighted order size"):
                adapter.place_live_order(
                    PaperOrderRequest(
                        "opinion_labs",
                        "77:YES:token-yes",
                        "BUY",
                        2,
                        0.5,
                        {"maker_amount_in_quote_token": "999"},
                    )
                )
        client.assert_not_called()

    def test_limit_and_market_semantics_cannot_be_overridden_by_metadata(self) -> None:
        opinion = OpinionAdapter(LIVE_CONFIG)
        with patch.object(opinion, "_create_clob_client") as opinion_client:
            with self.assertRaisesRegex(MarketConfigurationError, "must match the reviewed limit price"):
                opinion.place_live_order(
                    PaperOrderRequest(
                        "opinion_labs",
                        "77:YES:token-yes",
                        "BUY",
                        2,
                        0.5,
                        {"order_type": "market"},
                    )
                )
        opinion_client.assert_not_called()

        limitless = LimitlessAdapter(LIVE_CONFIG)
        with self.assertRaisesRegex(MarketConfigurationError, "must not discard a reviewed limit price"):
            limitless.place_live_order(
                PaperOrderRequest(
                    "limitless_exchange",
                    "market-one:YES",
                    "BUY",
                    2,
                    0.5,
                    {"order_type": "FOK"},
                )
            )

        xo = XOMarketAdapter(LIVE_CONFIG)
        with self.assertRaisesRegex(MarketConfigurationError, "cannot convert a limit order"):
            xo.place_live_order(
                PaperOrderRequest(
                    "xo_market",
                    "market-one:yes",
                    "BUY",
                    2,
                    0.5,
                    {"type": "market"},
                )
            )

        manifold = ManifoldAdapter(LIVE_CONFIG)
        with self.assertRaisesRegex(MarketConfigurationError, "does not support a wire-enforced limit"):
            manifold.place_live_order(
                PaperOrderRequest("manifold", "market-one:YES", "SELL", 2, 0.5)
            )

    def test_prophet_and_smarkets_reject_raw_wire_payloads_before_mutation(self) -> None:
        prophet = ProphetExchangeAdapter(LIVE_CONFIG)
        prophet_order = PaperOrderRequest(
            "prophet_exchange",
            "1:2:3:4",
            "BUY",
            2,
            0.5,
            {"prophet_exchange_order": {"quantity": 999}},
        )
        selection = {"strike_id": "strike-1"}
        with patch.object(prophet, "_validate_order", return_value=("1", "2", "3", "4", selection)), patch.object(
            prophet, "_request_json"
        ) as prophet_request:
            with self.assertRaisesRegex(MarketConfigurationError, "constructed from the reviewed order"):
                prophet.place_live_order(prophet_order)
        prophet_request.assert_not_called()

        smarkets = SmarketsAdapter(LIVE_CONFIG)
        with patch.object(smarkets, "_request_json") as smarkets_request:
            with self.assertRaisesRegex(MarketConfigurationError, "constructed from the reviewed order"):
                smarkets.place_live_order(
                    PaperOrderRequest(
                        "smarkets",
                        "market-1:contract-1",
                        "BUY",
                        2,
                        0.5,
                        {"smarkets_order": {"quantity": "999"}},
                    )
                )
        smarkets_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
