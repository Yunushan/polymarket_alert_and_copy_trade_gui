from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.storage import DEFAULT_CONFIG_PATH, load_config, save_config


def _dispatch_summary(dispatch: dict) -> dict:
    allowed = ("market_id", "contract_id", "side", "size", "limit_price", "tif", "approx_notional")
    summary = {}
    for key in allowed:
        if key in dispatch and (
            dispatch[key] is None or isinstance(dispatch[key], (str, int, float, bool))
        ):
            summary[key] = dispatch[key]
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List or reconcile non-replayable copy-trading dispatch outcomes.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Application config path (defaults to PREDICTION_MARKET_CONFIG_PATH or data/config.json).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list", help="List copy-activity entries needing reconciliation.")
    list_parser.add_argument("--all", action="store_true", help="Include conclusive and retryable entries.")
    resolve = commands.add_parser("resolve", help="Record a manual venue reconciliation.")
    resolve.add_argument("entry_id", help="Outbox entry id shown by the list command.")
    resolve.add_argument(
        "resolution",
        choices=("confirmed_dispatched", "confirmed_not_dispatched", "discard"),
        help=(
            "confirmed_dispatched closes the entry; confirmed_not_dispatched permits retry; "
            "discard closes it without retry."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "list":
        entries = [
            {
                "activity_key": entry.activity_key,
                "attempts": entry.attempts,
                "dispatch": _dispatch_summary(entry.dispatch),
                "entry_id": entry.id,
                "market_id": entry.market_id,
                "outcome_code": entry.outcome_code,
                "state": entry.state,
                "updated_at": entry.updated_at,
            }
            for entry in cfg.copy_activity_outbox
            if args.all or entry.state == "ambiguous"
        ]
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0

    entry = cfg.reconcile_ambiguous_copy_activity(args.entry_id, args.resolution)
    save_config(cfg, args.config)
    print(
        json.dumps(
            {
                "activity_key": entry.activity_key,
                "entry_id": entry.id,
                "outcome_code": entry.outcome_code,
                "state": entry.state,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
