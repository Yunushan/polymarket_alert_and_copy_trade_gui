from __future__ import annotations

import unittest

from market_adapters.errors import MarketConfigurationError
from market_adapters.identity import normalize_activity_identity, require_activity_identity


class ActivityIdentityTests(unittest.TestCase):
    def test_evm_identity_remains_canonical(self) -> None:
        wallet = "0x" + "AB" * 20
        self.assertEqual(normalize_activity_identity("polymarket", wallet), wallet.lower())

    def test_manifold_identity_requires_explicit_prefix_and_normalizes_case(self) -> None:
        self.assertEqual(
            normalize_activity_identity("manifold", "Manifold:Forecast_User-1"),
            "manifold:forecast_user-1",
        )
        self.assertIsNone(normalize_activity_identity("manifold", "Forecast_User-1"))

    def test_manifold_identity_rejects_path_control_characters(self) -> None:
        for value in ("manifold:../etc/passwd", "manifold:/root", "manifold:"):
            self.assertIsNone(normalize_activity_identity("manifold", value))
        with self.assertRaises(MarketConfigurationError):
            require_activity_identity("manifold", "manifold:../etc/passwd")

    def test_metadao_identity_requires_canonical_solana_base58_key(self) -> None:
        wallet = "11111111111111111111111111111111"
        self.assertEqual(
            normalize_activity_identity("metadao", wallet),
            f"solana:{wallet}",
        )
        self.assertEqual(
            normalize_activity_identity("metadao", f"SOLANA:{wallet}"),
            f"solana:{wallet}",
        )
        for value in ("0x" + "11" * 20, "solana:not-a-wallet", "1" * 31):
            self.assertIsNone(normalize_activity_identity("metadao", value))
        with self.assertRaises(MarketConfigurationError):
            require_activity_identity("metadao", "not-a-wallet")

    def test_dflow_identity_uses_the_same_canonical_solana_boundary(self) -> None:
        wallet = "11111111111111111111111111111111"
        self.assertEqual(
            normalize_activity_identity("dflow", f"SOLANA:{wallet}"),
            f"solana:{wallet}",
        )
        self.assertIsNone(normalize_activity_identity("dflow", "0x" + "11" * 20))
        with self.assertRaises(MarketConfigurationError):
            require_activity_identity("dflow", "not-a-wallet")


if __name__ == "__main__":
    unittest.main()
