"""Tests for ExtractionResultCache (ADR-074)."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from kebi.api.schemas.extract_place import EvidenceDTO, ExtractPlaceItem
from kebi.core.extraction.result_cache import (
    _KEY_PREFIX,
    ExtractionResultCache,
)
from kebi.core.places_v2 import PlaceCore

_CANONICAL_URL = "https://www.tiktok.com/@user/video/123"
# Tests use a fixed TTL; the production wiring layer pulls it from
# `config.extraction.result_cache_ttl_seconds`.
_TEST_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _make_item(
    provider_id: str = "google:abc",
    place_name: str = "Fuji Ramen",
) -> ExtractPlaceItem:
    """Build an ExtractPlaceItem with the minimum fields required by the schema."""
    return ExtractPlaceItem(
        place=PlaceCore(
            id="00000000-0000-0000-0000-000000000001",
            provider_id=provider_id,
            place_name=place_name,
        ),
        confidence=0.9,
        evidence=[EvidenceDTO(producer="llm_ner", medium="caption", snippet="…")],
    )


@pytest.fixture
def redis_mock() -> AsyncMock:
    """A redis-asyncio AsyncMock that emulates a tiny in-memory keyspace."""
    storage: dict[str, str] = {}

    async def _get(key: str) -> str | None:
        return storage.get(key)

    async def _set(key: str, value: str, ex: int | None = None) -> None:
        storage[key] = value

    async def _delete(*keys: str) -> int:
        n = 0
        for k in keys:
            n += int(storage.pop(k, None) is not None)
        return n

    redis = AsyncMock(spec=Redis)
    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis.delete = AsyncMock(side_effect=_delete)
    redis._storage = storage  # exposed for test introspection
    return redis


def _expected_key(url: str) -> str:
    return f"{_KEY_PREFIX}{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


class TestRoundTrip:
    async def test_set_then_get_returns_items(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        items = [_make_item(), _make_item("google:def", "Pizza Place")]
        await cache.set(_CANONICAL_URL, items)
        out = await cache.get(_CANONICAL_URL)
        assert out is not None
        assert len(out) == 2
        assert out[0].place.place_name == "Fuji Ramen"
        assert out[1].place.place_name == "Pizza Place"

    async def test_get_returns_none_on_miss(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        out = await cache.get("https://nothing.example.com/x")
        assert out is None

    async def test_set_uses_sha256_keyed_redis_set(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.set(_CANONICAL_URL, [_make_item()])
        assert _expected_key(_CANONICAL_URL) in redis_mock._storage

    async def test_set_applies_default_ttl(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.set(_CANONICAL_URL, [_make_item()])
        # redis.set was called with ex=_TEST_TTL_SECONDS
        _, kwargs = redis_mock.set.await_args
        assert kwargs.get("ex") == _TEST_TTL_SECONDS

    async def test_custom_ttl_is_honored(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=600)
        await cache.set(_CANONICAL_URL, [_make_item()])
        _, kwargs = redis_mock.set.await_args
        assert kwargs.get("ex") == 600


class TestEmptyItems:
    async def test_set_with_empty_list_is_noop(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.set(_CANONICAL_URL, [])
        # No write should have happened.
        redis_mock.set.assert_not_awaited()


class TestDelete:
    async def test_delete_evicts_key(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.set(_CANONICAL_URL, [_make_item()])
        assert _expected_key(_CANONICAL_URL) in redis_mock._storage
        await cache.delete(_CANONICAL_URL)
        assert _expected_key(_CANONICAL_URL) not in redis_mock._storage

    async def test_delete_missing_key_is_noop(self, redis_mock: AsyncMock) -> None:
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.delete(_CANONICAL_URL)  # no prior set
        # No raise.


class TestFailOpen:
    async def test_get_swallows_redis_error_returns_none(
        self, redis_mock: AsyncMock
    ) -> None:
        redis_mock.get = AsyncMock(side_effect=RuntimeError("redis down"))
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        out = await cache.get(_CANONICAL_URL)
        assert out is None

    async def test_set_swallows_redis_error(self, redis_mock: AsyncMock) -> None:
        redis_mock.set = AsyncMock(side_effect=RuntimeError("redis down"))
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.set(_CANONICAL_URL, [_make_item()])  # no raise

    async def test_delete_swallows_redis_error(self, redis_mock: AsyncMock) -> None:
        redis_mock.delete = AsyncMock(side_effect=RuntimeError("redis down"))
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        await cache.delete(_CANONICAL_URL)  # no raise

    async def test_corrupted_json_treated_as_miss(self, redis_mock: AsyncMock) -> None:
        key = _expected_key(_CANONICAL_URL)
        redis_mock._storage[key] = "not-valid-json"
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        out = await cache.get(_CANONICAL_URL)
        assert out is None

    async def test_non_list_payload_treated_as_miss(
        self, redis_mock: AsyncMock
    ) -> None:
        key = _expected_key(_CANONICAL_URL)
        redis_mock._storage[key] = json.dumps({"not": "a list"})
        cache = ExtractionResultCache(redis_mock, ttl_seconds=_TEST_TTL_SECONDS)
        out = await cache.get(_CANONICAL_URL)
        assert out is None
