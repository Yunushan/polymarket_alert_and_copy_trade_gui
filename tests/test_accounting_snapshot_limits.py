from __future__ import annotations

import csv
import io
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from polymarket import accounting
from polymarket.accounting import AccountingSnapshotLimitError, parse_accounting_snapshot_zip, reconcile_mdd_payload_with_accounting


def archive_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


def one_csv(content):
    return archive_bytes([("equity.csv", content)])


class AccountingSnapshotLimitTests(unittest.TestCase):
    def test_utf8_bom_crlf_and_quoted_newlines_are_supported(self):
        result = parse_accounting_snapshot_zip(one_csv(b'\xef\xbb\xbftimestamp,equity,note\r\n1,100,"first\r\nsecond"\r\n'))
        self.assertTrue(result["complete"])
        self.assertEqual(result["equity"]["base_equity_usd"], 100)
        self.assertEqual(result["files"][0]["rows"], 1)

    def test_latin1_fallback_preserves_numeric_values(self):
        result = parse_accounting_snapshot_zip(one_csv(b'equity,note\n123,caf\xe9\n'))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["equity"]["base_equity_usd"], 123)

    def test_small_row_limit_does_not_decompress_the_whole_member(self):
        raw = one_csv(b'equity\n' + b'1\n' * 1000000)
        original_read = zipfile.ZipExtFile.read
        requests = []
        returned = []

        def read(source, size=-1):
            requests.append(size)
            result = original_read(source, size)
            returned.append(len(result))
            return result

        with patch.object(zipfile.ZipExtFile, "read", read):
            result = parse_accounting_snapshot_zip(raw, max_rows_per_file=1)
        self.assertTrue(all(0 < size <= 16384 for size in requests))
        self.assertLess(sum(returned), 65536)
        self.assertEqual(result["equity"]["rows"], 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["files"][0]["truncated"])

    def test_compressed_archive_limit_is_checked_before_zip_parsing(self):
        with patch.object(accounting, "MAX_ACCOUNTING_ARCHIVE_BYTES", 4), patch.object(zipfile, "ZipFile") as parser:
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(b"12345")
        parser.assert_not_called()

    def test_declared_member_size_is_checked_before_expansion(self):
        raw = one_csv("equity\n" + "1\n" * 1000)
        with patch.object(accounting, "MAX_ACCOUNTING_MEMBER_BYTES", 32), patch.object(zipfile.ZipFile, "open") as opener:
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(raw)
        opener.assert_not_called()

    def test_aggregate_expanded_size_is_checked_before_expansion(self):
        raw = archive_bytes([("equity.csv", "equity\n12345\n"), ("positions.csv", "value\n12345\n")])
        with patch.object(accounting, "MAX_ACCOUNTING_EXPANDED_BYTES", 20), patch.object(zipfile.ZipFile, "open") as opener:
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(raw)
        opener.assert_not_called()

    def test_actual_stream_size_is_bounded_even_if_metadata_understates_it(self):
        info = zipfile.ZipInfo("equity.csv")
        info.file_size = 1
        fake_archive = MagicMock()
        fake_archive.__enter__.return_value = fake_archive
        fake_archive.infolist.return_value = [info]
        fake_archive.open.return_value = io.BytesIO(b"equity\n" + b"1\n" * 100)
        with patch.object(zipfile, "ZipFile", return_value=fake_archive), patch.object(accounting, "MAX_ACCOUNTING_MEMBER_BYTES", 16):
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(b"zip")

    def test_actual_reads_share_one_aggregate_budget(self):
        budget = {"remaining": 5}
        with accounting._BoundedArchiveReader(io.BytesIO(b"abc"), budget) as first:
            self.assertEqual(first.read(), b"abc")
        with accounting._BoundedArchiveReader(io.BytesIO(b"def"), budget) as second:
            with self.assertRaises(AccountingSnapshotLimitError):
                second.read()

    def test_too_many_csv_members_fail_instead_of_silent_partial_totals(self):
        raw = archive_bytes([(f"{number}.csv", "equity\n1\n") for number in range(3)])
        with patch.object(accounting, "MAX_ACCOUNTING_CSV_FILES", 2):
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(raw)

    def test_archive_member_count_includes_non_csv_members(self):
        raw = archive_bytes([("a.txt", "x"), ("b.txt", "x"), ("equity.csv", "equity\n1\n")])
        with patch.object(accounting, "MAX_ACCOUNTING_ARCHIVE_MEMBERS", 2):
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(raw)

    def test_non_csv_members_are_not_expanded(self):
        raw = archive_bytes([("ignored.txt", "x" * 10000), ("equity.csv", "equity\n1\n")])
        with patch.object(accounting, "MAX_ACCOUNTING_MEMBER_BYTES", 32):
            result = parse_accounting_snapshot_zip(raw)
        self.assertEqual(result["equity"]["base_equity_usd"], 1)

    def test_logical_record_limit_includes_quoted_newlines(self):
        for record in ('"' + "x" * 200 + '"', '"' + "x\n" * 100 + '"'):
            with self.subTest(record=record), patch.object(accounting, "MAX_ACCOUNTING_RECORD_CHARS", 64):
                with self.assertRaises(AccountingSnapshotLimitError):
                    parse_accounting_snapshot_zip(one_csv("equity,note\n1," + record + "\n"))

    def test_header_and_data_column_limits_are_enforced(self):
        with patch.object(accounting, "MAX_ACCOUNTING_COLUMNS", 2):
            with self.assertRaises(AccountingSnapshotLimitError):
                parse_accounting_snapshot_zip(one_csv("equity,a,b\n1,2,3\n"))
        with self.assertRaises(ValueError):
            parse_accounting_snapshot_zip(one_csv("equity\n1,2\n"))

    def test_duplicate_normalized_headers_and_casefold_member_names_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_accounting_snapshot_zip(one_csv("Equity,equity\n1,2\n"))
        raw = archive_bytes([("equity.csv", "equity\n1\n"), ("EQUITY.csv", "equity\n2\n")])
        with self.assertRaises(ValueError):
            parse_accounting_snapshot_zip(raw)

    def test_total_row_budget_applies_across_files(self):
        raw = archive_bytes([("equity.csv", "equity\n1\n2\n"), ("other.csv", "equity\n3\n4\n")])
        with patch.object(accounting, "MAX_ACCOUNTING_TOTAL_ROWS", 3):
            result = parse_accounting_snapshot_zip(raw)
        self.assertEqual(sum(file["rows"] for file in result["files"]), 3)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])

    def test_per_file_row_limit_cannot_exceed_the_hard_cap(self):
        with patch.object(accounting, "MAX_ACCOUNTING_ROWS_PER_FILE", 2):
            result = parse_accounting_snapshot_zip(one_csv("equity\n1\n2\n3\n"), max_rows_per_file=999999)
        self.assertEqual(result["equity"]["rows"], 2)
        self.assertFalse(result["complete"])
        with self.assertRaises(ValueError):
            parse_accounting_snapshot_zip(one_csv("equity\n1\n"), max_rows_per_file=0)

    def test_exact_row_limit_without_omitted_data_is_complete(self):
        result = parse_accounting_snapshot_zip(one_csv("equity\n1\n"), max_rows_per_file=1)
        self.assertTrue(result["complete"])

    def test_partial_snapshot_cannot_override_an_existing_mdd_base(self):
        snapshot = parse_accounting_snapshot_zip(one_csv("equity\n10000\n20000\n"), max_rows_per_file=1)
        payload = {"mdd_available": True, "mdd_pct": 50, "mdd_usd": 50, "equity_base_usd": 100,
                   "points_total": 2, "points": [{"value": 0}, {"value": -50}]}
        result = reconcile_mdd_payload_with_accounting(payload, snapshot)
        self.assertEqual(result["mdd_pct"], 50)
        self.assertEqual(result["equity_base_usd"], 100)
        self.assertFalse(result["accounting_snapshot"]["complete"])
        self.assertFalse(result["accounting_snapshot"]["reconciliation"]["mdd_pct_uses_accounting_base"])

    def test_invalid_and_empty_archives_do_not_claim_completeness(self):
        self.assertEqual(parse_accounting_snapshot_zip(b"invalid")["status"], "invalid_zip")
        self.assertFalse(parse_accounting_snapshot_zip(archive_bytes([]))["complete"])

    def test_nonfinite_equity_cannot_become_a_capital_base(self):
        for value in ("NaN", "inf", "-inf"):
            with self.subTest(value=value):
                result = parse_accounting_snapshot_zip(one_csv(f"equity\n{value}\n"))
                self.assertIsNone(result["equity"]["base_equity_usd"])

    def test_parallel_parsers_do_not_change_global_csv_limits(self):
        field_limit = csv.field_size_limit()
        raw = one_csv("equity\n123\n")
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(parse_accounting_snapshot_zip, [raw] * 20))
        self.assertTrue(all(result["equity"]["base_equity_usd"] == 123 for result in results))
        self.assertEqual(csv.field_size_limit(), field_limit)


if __name__ == "__main__":
    unittest.main()
