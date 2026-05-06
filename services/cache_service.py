from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Tuple


class TTLCache:
    """Small in-memory TTL cache for lightweight API acceleration."""

    def __init__(self, *, default_ttl: int = 20, max_items: int = 512):
        self.default_ttl = max(1, int(default_ttl))
        self.max_items = max(8, int(max_items))
        self._storage: Dict[Any, Tuple[float, Any]] = {}

    def get(self, key: Any) -> Optional[Any]:
        item = self._storage.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at <= time.time():
            self._storage.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any, *, ttl: Optional[int] = None) -> None:
        if len(self._storage) >= self.max_items:
            self._evict_expired()
            if len(self._storage) >= self.max_items:
                oldest_key = next(iter(self._storage.keys()))
                self._storage.pop(oldest_key, None)
        lifetime = max(1, int(ttl if ttl is not None else self.default_ttl))
        self._storage[key] = (time.time() + lifetime, value)

    def invalidate(self, predicate: Optional[Callable[[Any], bool]] = None) -> int:
        if predicate is None:
            size = len(self._storage)
            self._storage.clear()
            return size

        deleted = 0
        for key in list(self._storage.keys()):
            if predicate(key):
                self._storage.pop(key, None)
                deleted += 1
        return deleted

    def _evict_expired(self) -> None:
        now = time.time()
        for key in list(self._storage.keys()):
            expires_at, _ = self._storage[key]
            if expires_at <= now:
                self._storage.pop(key, None)

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._storage)
