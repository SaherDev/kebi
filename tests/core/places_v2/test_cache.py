"""Tests for RedisPlacesCache."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.core.places_v2.cache import RedisPlacesCache
from totoro_ai.core.places_v2.models import PlaceObject


def _make_cache(redis: MagicMock) -> RedisPlacesCache:
    return RedisPlacesCache(redis=redis)


@pytest.fixture
def redis_mock() -> MagicMock:
    mock = MagicMock()
    mock.mget = AsyncMock()
    mock.pipeline = MagicMock()
    return mock


class TestRedisPlacesCacheMget:
    async def test_empty_input_returns_empty(self, redis_mock: MagicMock) -> None:
        cache = _make_cache(redis_mock)
        result = await cache.mget([])
        assert result == {}
        redis_mock.mget.assert_not_called()

    async def test_returns_parsed_objects(self, redis_mock: MagicMock) -> None:
        obj = PlaceObject(place_name="Ramen Bar", provider_id="google:abc")
        redis_mock.mget = AsyncMock(return_value=[obj.model_dump_json(), None])
        cache = _make_cache(redis_mock)

        result = await cache.mget(["google:abc", "google:miss"])

        assert "google:abc" in result
        assert result["google:abc"].place_name == "Ramen Bar"
        assert "google:miss" not in result

    async def test_drops_invalid_json(self, redis_mock: MagicMock) -> None:
        redis_mock.mget = AsyncMock(return_value=[b"not-valid-json"])
        cache = _make_cache(redis_mock)

        result = await cache.mget(["google:bad"])
        assert result == {}

    async def test_redis_error_returns_empty(self, redis_mock: MagicMock) -> None:
        redis_mock.mget = AsyncMock(side_effect=Exception("Redis down"))
        cache = _make_cache(redis_mock)

        result = await cache.mget(["google:abc"])
        assert result == {}


class TestRedisPlacesCacheMset:
    async def test_skips_places_without_provider_id(
        self, redis_mock: MagicMock
    ) -> None:
        cache = _make_cache(redis_mock)
        pipeline_mock = AsyncMock()
        pipeline_mock.__aenter__ = AsyncMock(return_value=pipeline_mock)
        pipeline_mock.__aexit__ = AsyncMock(return_value=False)
        redis_mock.pipeline.return_value = pipeline_mock

        await cache.mset([PlaceObject(place_name="Anon")])
        pipeline_mock.set.assert_not_called()

    async def test_writes_with_ttl(self, redis_mock: MagicMock) -> None:
        cache = _make_cache(redis_mock)
        pipeline_mock = AsyncMock()
        pipeline_mock.__aenter__ = AsyncMock(return_value=pipeline_mock)
        pipeline_mock.__aexit__ = AsyncMock(return_value=False)
        redis_mock.pipeline.return_value = pipeline_mock

        obj = PlaceObject(place_name="Ramen Bar", provider_id="google:abc")
        await cache.mset([obj], ttl_seconds=3600)

        pipeline_mock.set.assert_called_once()
        call_kwargs = pipeline_mock.set.call_args
        assert "place_v2:google:abc" in call_kwargs.args
        assert call_kwargs.kwargs.get("ex") == 3600

    async def test_empty_list_is_noop(self, redis_mock: MagicMock) -> None:
        cache = _make_cache(redis_mock)
        await cache.mset([])
        redis_mock.pipeline.assert_not_called()

    async def test_redis_error_is_swallowed(self, redis_mock: MagicMock) -> None:
        cache = _make_cache(redis_mock)
        pipeline_mock = AsyncMock()
        pipeline_mock.__aenter__ = AsyncMock(return_value=pipeline_mock)
        pipeline_mock.__aexit__ = AsyncMock(return_value=False)
        pipeline_mock.set = MagicMock(side_effect=Exception("pipe error"))
        redis_mock.pipeline.return_value = pipeline_mock

        # Should not raise
        obj = PlaceObject(place_name="Ramen", provider_id="google:x")
        await cache.mset([obj])


class TestRedisPlacesCacheDeleteMany:
    async def test_empty_list_is_noop(self, redis_mock: MagicMock) -> None:
        redis_mock.delete = AsyncMock()
        cache = _make_cache(redis_mock)
        await cache.delete_many([])
        redis_mock.delete.assert_not_called()

    async def test_deletes_prefixed_keys(self, redis_mock: MagicMock) -> None:
        redis_mock.delete = AsyncMock(return_value=2)
        cache = _make_cache(redis_mock)
        await cache.delete_many(["google:a", "google:b"])
        redis_mock.delete.assert_awaited_once_with(
            "place_v2:google:a", "place_v2:google:b"
        )

    async def test_redis_error_is_swallowed(self, redis_mock: MagicMock) -> None:
        redis_mock.delete = AsyncMock(side_effect=Exception("Redis down"))
        cache = _make_cache(redis_mock)
        # Should not raise
        await cache.delete_many(["google:a"])
