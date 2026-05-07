"""Tests for ExtractionStatusRepository (US2 + US3)."""

import json

from kebi.core.extraction.status_repository import ExtractionStatusRepository
from kebi.providers.cache import (
    CacheBackend,  # noqa: F401 (Protocol isinstance check)
)

# ---------------------------------------------------------------------------
# In-memory stub satisfying CacheBackend Protocol
# ---------------------------------------------------------------------------


class InMemoryCacheBackend:
    """Minimal in-memory CacheBackend for testing. TTL is not enforced."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store[key] = value


# ---------------------------------------------------------------------------
# US2 — safe read behavior
# ---------------------------------------------------------------------------


async def test_read_missing_key_returns_none() -> None:
    """Reading a nonexistent key returns None — no error raised."""
    repo = ExtractionStatusRepository(cache=InMemoryCacheBackend())
    result = await repo.read("nonexistent-id")
    assert result is None


async def test_read_after_write_returns_dict() -> None:
    """Write then read returns the original payload dict."""
    repo = ExtractionStatusRepository(cache=InMemoryCacheBackend())
    payload = {"extraction_status": "saved", "places": []}
    await repo.write("req-1", payload)
    result = await repo.read("req-1")
    assert result == payload


async def test_read_returns_none_for_different_request_id() -> None:
    """Writing for one request_id does not affect another."""
    repo = ExtractionStatusRepository(cache=InMemoryCacheBackend())
    await repo.write("req-a", {"extraction_status": "saved"})
    result = await repo.read("req-b")
    assert result is None


# ---------------------------------------------------------------------------
# US3 — key format, TTL, Protocol compliance
# ---------------------------------------------------------------------------


async def test_key_format_uses_extraction_prefix() -> None:
    """Write uses key format 'extraction:{request_id}'."""

    class CapturingBackend:
        def __init__(self) -> None:
            self.last_key: str | None = None

        async def get(self, key: str) -> str | None:
            return None

        async def set(self, key: str, value: str, ttl: int) -> None:
            self.last_key = key

    backend = CapturingBackend()
    repo = ExtractionStatusRepository(cache=backend)  # type: ignore[arg-type]
    await repo.write("my-req-id", {"extraction_status": "failed"})
    # ADR-063: prefix bumped to v2 alongside the two-level ExtractPlaceResponse shape.
    assert backend.last_key == "extraction:v2:my-req-id"


async def test_write_uses_default_ttl_of_3600() -> None:
    """write() passes ttl=3600 by default."""

    class CapturingBackend:
        def __init__(self) -> None:
            self.last_ttl: int | None = None

        async def get(self, key: str) -> str | None:
            return None

        async def set(self, key: str, value: str, ttl: int) -> None:
            self.last_ttl = ttl

    backend = CapturingBackend()
    repo = ExtractionStatusRepository(cache=backend)  # type: ignore[arg-type]
    await repo.write("req-ttl", {"x": "y"})
    assert backend.last_ttl == 3600


async def test_write_accepts_custom_ttl() -> None:
    """write() forwards custom ttl to cache.set."""

    class CapturingBackend:
        def __init__(self) -> None:
            self.last_ttl: int | None = None

        async def get(self, key: str) -> str | None:
            return None

        async def set(self, key: str, value: str, ttl: int) -> None:
            self.last_ttl = ttl

    backend = CapturingBackend()
    repo = ExtractionStatusRepository(cache=backend)  # type: ignore[arg-type]
    await repo.write("req-ttl", {"x": "y"}, ttl=7200)
    assert backend.last_ttl == 7200


async def test_payload_is_json_serialized_in_cache() -> None:
    """Cache stores JSON string, not raw dict."""

    class CapturingBackend:
        def __init__(self) -> None:
            self.last_value: str | None = None

        async def get(self, key: str) -> str | None:
            return None

        async def set(self, key: str, value: str, ttl: int) -> None:
            self.last_value = value

    backend = CapturingBackend()
    repo = ExtractionStatusRepository(cache=backend)  # type: ignore[arg-type]
    await repo.write("req-json", {"status": "ok", "count": 3})
    assert isinstance(backend.last_value, str)
    parsed = json.loads(backend.last_value)
    assert parsed == {"status": "ok", "count": 3}


async def test_in_memory_backend_satisfies_protocol() -> None:
    """InMemoryCacheBackend is a runtime-checkable CacheBackend."""
    backend = InMemoryCacheBackend()
    assert isinstance(backend, CacheBackend)
