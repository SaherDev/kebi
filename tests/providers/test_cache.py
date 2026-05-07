"""Tests for CacheBackend Protocol and RedisCacheBackend structural compliance (US3)."""

from kebi.providers.cache import CacheBackend
from kebi.providers.redis_cache import RedisCacheBackend

# ---------------------------------------------------------------------------
# In-memory stub for Protocol verification
# ---------------------------------------------------------------------------


class InMemoryCacheBackend:
    """Minimal in-memory CacheBackend for structural testing."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store[key] = value


# ---------------------------------------------------------------------------
# US3 — Protocol compliance
# ---------------------------------------------------------------------------


def test_in_memory_stub_satisfies_cache_backend_protocol() -> None:
    """InMemoryCacheBackend is a structural CacheBackend (runtime_checkable)."""
    backend = InMemoryCacheBackend()
    assert isinstance(backend, CacheBackend)


def test_redis_cache_backend_satisfies_cache_backend_protocol() -> None:
    """RedisCacheBackend satisfies CacheBackend Protocol — no network call needed."""
    backend = RedisCacheBackend(url="redis://localhost:6379/0")
    assert isinstance(backend, CacheBackend)


# ---------------------------------------------------------------------------
# InMemoryCacheBackend behavior (used as test double in repository tests)
# ---------------------------------------------------------------------------


async def test_in_memory_get_missing_key_returns_none() -> None:
    backend = InMemoryCacheBackend()
    result = await backend.get("missing")
    assert result is None


async def test_in_memory_set_and_get_round_trip() -> None:
    backend = InMemoryCacheBackend()
    await backend.set("key1", "hello", ttl=60)
    result = await backend.get("key1")
    assert result == "hello"


async def test_in_memory_set_overwrites_existing_value() -> None:
    backend = InMemoryCacheBackend()
    await backend.set("k", "first", ttl=60)
    await backend.set("k", "second", ttl=60)
    result = await backend.get("k")
    assert result == "second"


async def test_in_memory_different_keys_are_independent() -> None:
    backend = InMemoryCacheBackend()
    await backend.set("a", "value-a", ttl=60)
    await backend.set("b", "value-b", ttl=60)
    assert await backend.get("a") == "value-a"
    assert await backend.get("b") == "value-b"
