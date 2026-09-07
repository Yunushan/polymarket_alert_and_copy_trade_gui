from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from typing import Any


class BoundedJsonCache:
    """Thread-safe LRU with bounded retained JSON bytes and per-entry expiry.

    Values are encoded on write and decoded on read, so callers cannot mutate
    another request's cached history. Network work is never done under its lock.
    The byte limit measures stored keys/payloads, not total process memory.
    """

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("Cache limits must be positive.")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[bytes, tuple[float, float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _encode(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")

    def _remove(self, key: bytes) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= len(key) + len(entry[2])

    def _expire(self, now: float) -> None:
        for key, (_stored, expires, _value) in list(self._entries.items()):
            if expires <= now:
                self._remove(key)

    def get(self, key: Any, *, max_age_seconds: float) -> Any:
        encoded_key = self._encode(key)
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            entry = self._entries.get(encoded_key)
            if max_age_seconds <= 0 or entry is None:
                return None
            if now - entry[0] >= max_age_seconds:
                return None
            self._entries.move_to_end(encoded_key)
            encoded_value = entry[2]
        return json.loads(encoded_value)

    def put(self, key: Any, value: Any, *, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            return False
        encoded_key, encoded_value = self._encode(key), self._encode(value)
        size = len(encoded_key) + len(encoded_value)
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            self._remove(encoded_key)
            if size > self.max_bytes:
                return False
            while self._entries and (len(self._entries) >= self.max_entries or self._bytes + size > self.max_bytes):
                self._remove(next(iter(self._entries)))
            self._entries[encoded_key] = (now, now + ttl_seconds, encoded_value)
            self._bytes += size
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._expire(time.monotonic())
            return {"entries": len(self._entries), "serialized_bytes": self._bytes,
                    "max_entries": self.max_entries, "max_serialized_bytes": self.max_bytes}
