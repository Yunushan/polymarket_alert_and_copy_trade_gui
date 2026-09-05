import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from polymarket import mdd
from polymarket.memory_cache import BoundedJsonCache


class MddMemoryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        mdd.clear_mdd_input_cache()
        self.addCleanup(mdd.clear_mdd_input_cache)

    def test_expired_entries_are_removed_when_an_unrelated_key_is_written(self) -> None:
        cache = BoundedJsonCache(max_entries=2000, max_bytes=100000)
        with patch("polymarket.memory_cache.time.monotonic", return_value=1000):
            for index in range(1000):
                cache.put(index, {"value": index}, ttl_seconds=60)
        with patch("polymarket.memory_cache.time.monotonic", return_value=2000):
            cache.put(1001, {"value": 1001}, ttl_seconds=60)
            self.assertEqual(cache.stats()["entries"], 1)

    def test_expiry_is_also_enforced_on_read_at_exact_deadline(self) -> None:
        cache = BoundedJsonCache(max_entries=2, max_bytes=1000)
        with patch("polymarket.memory_cache.time.monotonic", return_value=10):
            cache.put("a", {}, ttl_seconds=60)
        with patch("polymarket.memory_cache.time.monotonic", return_value=70):
            self.assertIsNone(cache.get("a", max_age_seconds=100))
            self.assertEqual(cache.stats()["serialized_bytes"], 0)

    def test_shorter_requested_ttl_and_zero_ttl_do_not_reuse_older_values(self) -> None:
        cache = BoundedJsonCache(max_entries=2, max_bytes=1000)
        with patch("polymarket.memory_cache.time.monotonic", return_value=10):
            cache.put("a", {}, ttl_seconds=60)
        with patch("polymarket.memory_cache.time.monotonic", return_value=20):
            self.assertIsNone(cache.get("a", max_age_seconds=5))
            self.assertIsNone(cache.get("a", max_age_seconds=0))
            self.assertEqual(cache.get("a", max_age_seconds=60), {})
            self.assertFalse(cache.put("b", {}, ttl_seconds=0))

    def test_entry_bound_uses_least_recently_read_eviction(self) -> None:
        cache = BoundedJsonCache(max_entries=2, max_bytes=1000)
        for key in ("a", "b"):
            cache.put(key, {"key": key}, ttl_seconds=60)
        cache.get("a", max_age_seconds=60)
        cache.put("c", {}, ttl_seconds=60)
        self.assertIsNone(cache.get("b", max_age_seconds=60))
        self.assertIsNotNone(cache.get("a", max_age_seconds=60))

    def test_byte_bound_and_oversized_replacement_do_not_retain_stale_value(self) -> None:
        cache = BoundedJsonCache(max_entries=10, max_bytes=100)
        for key in ("a", "b", "c", "d"):
            cache.put(key, {"value": "x" * 30}, ttl_seconds=60)
            self.assertLessEqual(cache.stats()["serialized_bytes"], 100)
        self.assertFalse(cache.put("d", {"value": "x" * 200}, ttl_seconds=60))
        self.assertIsNone(cache.get("d", max_age_seconds=60))

    def test_nested_values_are_isolated_from_writers_and_readers(self) -> None:
        cache = BoundedJsonCache(max_entries=2, max_bytes=1000)
        value = {"rows": [{"value": 1}]}
        cache.put("a", value, ttl_seconds=60)
        value["rows"][0]["value"] = 2
        first = cache.get("a", max_age_seconds=60)
        first["rows"][0]["value"] = 3
        self.assertEqual(cache.get("a", max_age_seconds=60)["rows"][0]["value"], 1)

    def test_concurrent_reads_writes_and_clear_preserve_bounds(self) -> None:
        cache = BoundedJsonCache(max_entries=16, max_bytes=2048)

        def exercise(worker: int) -> None:
            for index in range(200):
                cache.put((worker, index), {"value": index}, ttl_seconds=60)
                cache.get((worker, index), max_age_seconds=60)
                if index % 50 == 0:
                    cache.clear()
                state = cache.stats()
                self.assertLessEqual(state["entries"], 16)
                self.assertGreaterEqual(state["serialized_bytes"], 0)
                self.assertLessEqual(state["serialized_bytes"], 2048)

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(exercise, range(8)))

    def test_mdd_inputs_obey_cache_limits_and_copy_isolation(self) -> None:
        with patch.object(mdd, "_fetch_closed_positions", return_value=[{"realizedPnl": 30}]), patch.object(
            mdd, "_fetch_open_positions", return_value=[]
        ), patch.object(mdd, "_fetch_activity_events", return_value=[]), patch.object(mdd, "_fetch_trade_rows", return_value=[]):
            for index in range(300):
                mdd.fetch_mdd_inputs("0x" + format(index + 1, "040x"), cache_ttl_seconds=60)
            state = mdd._INPUT_CACHE.stats()
            self.assertEqual(state["entries"], 128)
            wallet = "0x" + format(300, "040x")
            data = mdd.fetch_mdd_inputs(wallet, cache_ttl_seconds=60)
            self.assertTrue(data.cache_hit)
            data.closed_positions[0]["realizedPnl"] = -100
            self.assertEqual(mdd.fetch_mdd_inputs(wallet, cache_ttl_seconds=60).closed_positions[0]["realizedPnl"], 30)

    def test_price_history_cache_is_bounded_and_nested_values_isolated(self) -> None:
        options = {"start_ts": 1, "end_ts": 2, "interval": "1h", "fidelity": 60, "cache_ttl_seconds": 60}
        with patch.object(mdd.clob_rest, "get_batch_price_history", return_value={"history": {"x": [{"p": 0.5}]}}):
            for index in range(300):
                mdd._batch_price_history_cached([str(index)], **options)
            self.assertEqual(mdd._PRICE_HISTORY_CACHE.stats()["entries"], 128)
            value, hit = mdd._batch_price_history_cached(["299"], **options)
            self.assertTrue(hit)
            value["history"]["x"][0]["p"] = -1
            value, _hit = mdd._batch_price_history_cached(["299"], **options)
            self.assertEqual(value["history"]["x"][0]["p"], 0.5)


if __name__ == "__main__":
    unittest.main()
