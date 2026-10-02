import time
from collections import OrderedDict
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """Small in-process TTL + LRU cache. For multiple instances, swap for Redis."""

    def __init__(self, ttl_s: float, max_entries: int, clock=time.monotonic):
        self._ttl, self._max, self._clock = ttl_s, max_entries, clock
        self._data: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def get(self, key: str) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < self._clock():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: V) -> None:
        self._data[key] = (self._clock() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
