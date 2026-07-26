"""A small, generic, in-memory LRU cache.

Used by `app.indicators.engine.IndicatorEngine` to avoid recomputing an
indicator for a (DataFrame, name, params) combination it has already
seen, but deliberately has no indicator-specific knowledge — it is a
plain key/value store any other layer can reuse.
"""

from collections import OrderedDict
from typing import Generic, TypeVar

KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")


class LRUCache(Generic[KeyT, ValueT]):
    """A bounded, least-recently-used-eviction key/value cache.

    Not thread-safe: callers running across multiple threads must
    synchronize externally. Fine for the single-event-loop async
    contexts this project uses the cache from.
    """

    def __init__(self, max_size: int) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._store: OrderedDict[KeyT, ValueT] = OrderedDict()

    def get(self, key: KeyT) -> ValueT | None:
        """Return the cached value for `key`, or `None` on a miss.

        A hit marks `key` as most-recently-used.
        """

        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def set(self, key: KeyT, value: ValueT) -> None:
        """Store `value` under `key`, evicting the least-recently-used
        entry if the cache is now over capacity.
        """

        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        """Drop every cached entry."""

        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
