from __future__ import annotations

import unittest
from pathlib import Path

from market_adapters import MARKET_IDS, VERIFIED_BLOCKERS


ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
REQUIRED_HEADERS = (
    "Market",
    "Adapter",
    "Alerts",
    "Read-only data",
    "Paper trading",
    "Live trading",
    "Copy trading",
    "API required",
    "Credentials required",
    "Region/KYC limitation",
)
CONCRETE_REQUIREMENT_VALUES = (
    "Required",
    "Yes",
    "No",
    "Live trading only",
    "Guarded, off by default",
    "Live/WebSocket only",
    "Live signed orders only",
    "Subgraph endpoint required",
    "Not required",
    "Trading may be region/KYC limited",
    "Exchange account/API keys",
    "Account required for trading",
    "Region/account limited",
    "Brokerage account required",
    "Region/KYC limited",
    "Account required",
    "IBKR account required",
    "Exchange/broker account required",
    "Broker/data entitlement required",
    "Exchange account required",
    "Crypto.com account required",
    "Wallet required for trading",
    "Jurisdiction varies",
    "API credentials required",
    "No API key; wallet/collateral required only for future live chain flow",
    "No API key; wallet required only for future live chain flow",
    "No API key; wallet/settlement required only for future live chain flow",
    "No API key for reads; externally signed wallet payload required for live orders",
    "No API key; externally signed wallet transaction required for live orders",
    "Optional API key",
    "Live trading only",
    "Not KYC limited",
    "Account/API token required",
    "API key required",
    "Not trading/KYC limited",
    "Account/export access required",
    "Program access required",
    "Program access limited",
    "IEM account required",
    "Eligibility limited",
    "Wallet/personhood required",
    "Identity/jurisdiction limited",
    "Account or wallet required",
    "Gemini account required",
    "Region limited",
    "Solana RPC; externally signed wallet transaction required for live orders",
    "Devnet example; production deployment must be reviewed",
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
    "dflow",
    "xmarket",
    "probable",
    "matchbook",
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
    "scicast",
}
VERIFIED_BLOCKED_MARKETS = set(VERIFIED_BLOCKERS)


def matrix_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    in_matrix = False
    for line in text.splitlines():
        if line.startswith("| Market | Adapter |"):
            in_matrix = True
            continue
        if not in_matrix:
            continue
        if not line.startswith("| "):
            break
        if line.startswith("| ---"):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


class ReadmeCapabilityMatrixTests(unittest.TestCase):
    def test_readme_capability_matrix_has_required_headers(self) -> None:
        text = README.read_text(encoding="utf-8")

        self.assertIn("## Market Capability Matrix", text)
        for header in REQUIRED_HEADERS:
            self.assertIn(header, text)

    def test_readme_capability_matrix_covers_all_catalog_markets(self) -> None:
        text = README.read_text(encoding="utf-8")

        for market_id in MARKET_IDS:
            self.assertIn(f"`{market_id}`", text)

    def test_readme_capability_matrix_has_no_tbd_values(self) -> None:
        text = README.read_text(encoding="utf-8")

        self.assertNotIn("TBD", text)
        rows = matrix_rows(text)
        self.assertEqual(len(rows), len(MARKET_IDS))
        for row in rows:
            self.assertEqual(len(row), len(REQUIRED_HEADERS), row)
            for value in row[-3:]:
                self.assertNotIn("TBD", value)
                self.assertIn(value, CONCRETE_REQUIREMENT_VALUES, row)

    def test_readme_marks_non_implemented_markets_as_stubs(self) -> None:
        text = README.read_text(encoding="utf-8")

        for line in text.splitlines():
            if not line.startswith("| "):
                continue
            if any(f"`{market_id}`" in line for market_id in IMPLEMENTED_MARKETS):
                self.assertIn("| Implemented |", line)
                continue
            if any(f"`{market_id}`" in line for market_id in VERIFIED_BLOCKED_MARKETS):
                self.assertIn("| Verified blocked |", line)
                continue
            if any(f"`{market_id}`" in line for market_id in MARKET_IDS):
                self.assertIn("| Stub |", line)


if __name__ == "__main__":
    unittest.main()

