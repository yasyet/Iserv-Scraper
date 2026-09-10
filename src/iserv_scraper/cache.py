"""A tiny thread-safe TTL cache (same design as in the WebUntis scraper)."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl: float = 120.0, enabled: bool = True) -> None:
        self.ttl = ttl
        self.enabled = enabled
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._store[key] = (time.monotonic() + (ttl if ttl is not None else self.ttl), value)

    def get_or_set(self, key: str, factory: Callable[[], T], ttl: float | None = None) -> T:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        self.set(key, value, ttl)
        return value

    def invalidate(self, prefix: str | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._store.clear()
                return
            for key in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(key, None)
