from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from core.models import AppConfig, CopyActivityOutboxEntry
from core.storage import load_config, save_config
from scripts.reconcile_copy_activity import main


def _ambiguous_entry(*, marker: str = "private-activity-marker") -> CopyActivityOutboxEntry:
    entry = CopyActivityOutboxEntry(
        id="entry-1",
        watch_id="watch-1",
        activity_key="tx:abc",
        activity={"transactionHash": "abc", "pseudonym": marker},
        market_id="polymarket",
        state="ambiguous",
        attempts=1,
        outcome_code="live_dispatch_started",
        outcome_message=marker,
        dispatch={
            "market_id": "polymarket",
            "contract_id": "token-1",
            "side": "BUY",
            "size": 1.0,
            "limit_price": 0.5,
            "tif": "FOK",
        },
    )
    entry.dispatch["private_detail"] = marker
    return entry


class CopyActivityReconciliationTests(unittest.TestCase):
    def test_list_outputs_only_reconciliation_fields(self) -> None:
        marker = "private-activity-marker"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(AppConfig(copy_activity_outbox=[_ambiguous_entry(marker=marker)]), path)
            output = io.StringIO()

            with redirect_stdout(output):
                result = main(["--config", str(path), "list"])

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload[0]["entry_id"], "entry-1")
        self.assertEqual(payload[0]["state"], "ambiguous")
        self.assertEqual(payload[0]["dispatch"]["contract_id"], "token-1")
        self.assertNotIn(marker, output.getvalue())
        self.assertNotIn("activity", payload[0])
        self.assertNotIn("outcome_message", payload[0])

    def test_confirmed_dispatch_closes_without_retry(self) -> None:
        self._assert_resolution("confirmed_dispatched", "completed", "manual_dispatch_confirmed")

    def test_confirmed_not_dispatched_explicitly_allows_retry(self) -> None:
        self._assert_resolution("confirmed_not_dispatched", "retryable", "manual_dispatch_cleared")

    def test_discard_closes_without_retry(self) -> None:
        self._assert_resolution("discard", "rejected", "manual_dispatch_discarded")

    def test_save_failure_leaves_ambiguous_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(AppConfig(copy_activity_outbox=[_ambiguous_entry()]), path)
            original = path.read_bytes()

            with patch(
                "scripts.reconcile_copy_activity.save_config",
                side_effect=OSError("do-not-echo-storage-detail"),
            ):
                with self.assertRaises(OSError):
                    main(
                        [
                            "--config",
                            str(path),
                            "resolve",
                            "entry-1",
                            "confirmed_not_dispatched",
                        ]
                    )

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(load_config(path).copy_activity_outbox[0].state, "ambiguous")

    def _assert_resolution(self, resolution: str, state: str, outcome_code: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            save_config(AppConfig(copy_activity_outbox=[_ambiguous_entry()]), path)
            output = io.StringIO()

            with redirect_stdout(output):
                result = main(["--config", str(path), "resolve", "entry-1", resolution])

            stored = load_config(path).copy_activity_outbox[0]

        self.assertEqual(result, 0)
        self.assertEqual(stored.state, state)
        self.assertEqual(stored.outcome_code, outcome_code)
        self.assertEqual(json.loads(output.getvalue())["state"], state)


if __name__ == "__main__":
    unittest.main()
