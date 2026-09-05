from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


# https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings
LEADERBOARD_MAX_OFFSET = 1000
PNL_VOLUME_BASIS = "pnl_usd / volume_usd * 100; not return on invested capital"


def performance_ratio_metadata(value: Any) -> dict[str, Any]:
    return {"pnl_volume_pct": value, "roi_pct_basis": PNL_VOLUME_BASIS}


def wallet_membership_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    wallets = set()
    for row in rows:
        wallet = next((str(row[key]).strip().lower() for key in (
            "wallet", "proxyWallet", "proxy_wallet", "address", "userAddress"
        ) if row.get(key)), "")
        if not wallet:
            return ""
        wallets.add(wallet)
    if not wallets:
        return ""
    return hashlib.sha256(json.dumps(sorted(wallets), separators=(",", ":")).encode("utf-8")).hexdigest()
