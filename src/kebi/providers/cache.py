"""CacheBackend Protocol — ADR-038 abstraction for all cache implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for async key-value cache with TTL support (ADR-038).

    Any class implementing get() and set() satisfies this Protocol.
    The active implementation is selected at startup — never import
    RedisCacheBackend directly outside of providers/ or deps.py.
    """

    async def get(self, key: str) -> str | None:
        """Return stored value for key, or None if missing/expired."""
        ...

    async def set(self, key: str, value: str, ttl: int) -> None:
        """Store value at key with TTL in seconds."""
        ...
