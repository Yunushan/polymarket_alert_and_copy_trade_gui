from __future__ import annotations

import unittest
from pathlib import Path

from market_adapters import MARKET_IDS, VERIFIED_BLOCKERS


ROOT = Path(__file__).resolve().parent.parent
BLOCKERS = ROOT / "docs" / "BLOCKERS.md"
REQUIRED_HEADERS = (
    "# Blockers",
    "## Summary",
    "## 2026-05-26 Re-Audit Notes",
    "## Market Blockers",
    "## Implementation Rules For Clearing A Blocker",
)
REQUIRED_COLUMNS = (
    "Market id",
    "Current adapter",
    "Blocker",
    "Required before full support",
    "Reference",
)
IMPLEMENTED_MARKETS = {
    "betmgm",
    "polymarket",
    "blinq",
    "kalshi",
    "predictit",
    "manifold",
    "metaculus",
    "good_judgment_open",
    "limitless_exchange",
    "sx_bet",
    "azuro",
    "augur",
    "reality_eth_markets",
    "omen",
    "gnosis_prediction_markets",
    "zeitgeist",
    "myriad_markets",
    "xo_market",
    "opinion_labs",
    "gemini_titan",
    "predict_fun",
    "betfair_exchange",
    "crypto_com_predict",
    "draftkings_predictions",
    "fanatics_markets",
    "fanduel_predicts",
    "coinbase_prediction_markets",
    "robinhood_prediction_markets",
    "kalshi_via_robinhood",
    "context_v2",
    "smarkets",
    "thales_market",
    "metadao",
    "seer",
    "hyperliquid",
    "trueo",
    "zeitgeist_sdk_markets",
    "zeitgeist_prediction_pools",
    "ibkr_forecasttrader",
    "forecastex",
    "cme_prediction_markets",
    "dflow",
    "xmarket",
    "probable",
    "matchbook",
    "drift_bet",
    "frenzy_finance",
    "space",
    "hedgehog_markets",
    "prophet_exchange",
    "prdt_finance",
    "zetarium_world",
    "lamas_finance",
    "nadex",
    "iowa_electronic_markets",
    "hypermind",
    "scicast",
}
VERIFIED_BLOCKED_MARKETS = set(VERIFIED_BLOCKERS)


class BlockersDocTests(unittest.TestCase):
    def test_blockers_doc_has_required_sections_and_columns(self) -> None:
        text = BLOCKERS.read_text(encoding="utf-8")

        for header in REQUIRED_HEADERS:
            self.assertIn(header, text)
        for column in REQUIRED_COLUMNS:
            self.assertIn(column, text)

    def test_blockers_doc_covers_every_catalog_market(self) -> None:
        text = BLOCKERS.read_text(encoding="utf-8")

        for market_id in MARKET_IDS:
            self.assertIn(f"`{market_id}`", text)

    def test_blockers_doc_marks_non_implemented_markets_as_stubs(self) -> None:
        text = BLOCKERS.read_text(encoding="utf-8")

        for line in text.splitlines():
            if not line.startswith("| `"):
                continue
            if any(f"`{market_id}`" in line for market_id in IMPLEMENTED_MARKETS):
                self.assertIn("| Implemented |", line)
                continue
            if any(f"`{market_id}`" in line for market_id in VERIFIED_BLOCKED_MARKETS):
                self.assertIn("| Verified blocked |", line)
                blocked_id = next(market_id for market_id in VERIFIED_BLOCKED_MARKETS if f"`{market_id}`" in line)
                expected_review = str(VERIFIED_BLOCKERS[blocked_id].get("last_reviewed") or "2026-05-26")
                self.assertIn(f"Verified {expected_review}", line)
                continue
            if any(f"`{market_id}`" in line for market_id in MARKET_IDS):
                self.assertIn("| Stub |", line)

    def test_blockers_doc_keeps_live_trading_disabled_rule(self) -> None:
        text = BLOCKERS.read_text(encoding="utf-8")

        self.assertIn("Live trading remains disabled by default", text)
        self.assertIn("Keep paper/dry-run mode as default", text)
        self.assertIn("Never commit credentials", text)

    def test_article35_reaudit_records_candidate_decisions(self) -> None:
        text = BLOCKERS.read_text(encoding="utf-8")

        self.assertIn("Frenzy Finance", text)
        self.assertIn("BetIntent", text)
        self.assertIn("`context_v2`", text)
        self.assertIn("current API reference", text)
        self.assertIn("`hyperliquid`", text)
        self.assertIn("outcomeMeta", text)
        self.assertIn("`thales_market`", text)
        self.assertIn("contract integration", text)
        self.assertIn("`smarkets`", text)
        self.assertIn("written approval", text)


if __name__ == "__main__":
    unittest.main()
