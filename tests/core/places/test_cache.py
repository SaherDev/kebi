"""Unit tests for PlacesCache — both Tier 2 (geo) and Tier 3 (enrichment)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from kebi.core.config import get_config
from kebi.core.places.cache import PlacesCache
from kebi.core.places.models import GeoData, PlaceEnrichment

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_geo(lat: float = 13.7, lng: float = 100.5, address: str = "Siam") -> GeoData:
    return GeoData(
        lat=lat,
        lng=lng,
        address=address,
        cached_at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
    )


def _make_enrichment(rating: float = 4.3) -> PlaceEnrichment:
    return PlaceEnrichment(
        hours={
            "monday": "09:00-18:00",
            "tuesday": "09:00-18:00",
            "timezone": "Asia/Bangkok",
        },
        rating=rating,
        phone="+66 2 000 0000",
        fetched_at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
    )


def _make_mock_redis() -> tuple[MagicMock, MagicMock]:
    """Return (redis_mock, pipeline_mock).

    The pipeline is a sync MagicMock (SET calls queue synchronously) whose
    `execute()` is async. The outer `redis.mget` is async.
    """
    pipe = MagicMock()
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[True, True])

    redis_mock = MagicMock()
    redis_mock.mget = AsyncMock(return_value=[])
    redis_mock.pipeline = MagicMock(return_value=pipe)
    return redis_mock, pipe


def _expected_ttl() -> int:
    return get_config().places.cache_ttl_days * 86400


# ---------------------------------------------------------------------------
# get_geo_batch
# ---------------------------------------------------------------------------


async def test_get_geo_batch_all_hit_returns_geodata_per_key() -> None:
    redis_mock, _ = _make_mock_redis()
    geo_a = _make_geo(lat=1.0, address="A")
    geo_b = _make_geo(lat=2.0, address="B")
    redis_mock.mget = AsyncMock(
        return_value=[geo_a.model_dump_json(), geo_b.model_dump_json()]
    )
    cache = PlacesCache(redis_mock)

    result = await cache.get_geo_batch(["google:a", "google:b"])

    redis_mock.mget.assert_awaited_once_with(
        ["places:geo:google:a", "places:geo:google:b"]
    )
    assert result["google:a"] is not None
    assert result["google:a"].lat == 1.0
    assert result["google:b"] is not None
    assert result["google:b"].address == "B"


async def test_get_geo_batch_partial_miss_returns_none_for_missing() -> None:
    redis_mock, _ = _make_mock_redis()
    geo_a = _make_geo(lat=1.0)
    redis_mock.mget = AsyncMock(return_value=[geo_a.model_dump_json(), None])
    cache = PlacesCache(redis_mock)

    result = await cache.get_geo_batch(["google:hit", "google:miss"])

    assert result["google:hit"] is not None
    assert result["google:miss"] is None


async def test_get_geo_batch_empty_input_short_circuits_without_redis_call() -> None:
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    result = await cache.get_geo_batch([])

    assert result == {}
    redis_mock.mget.assert_not_called()


# ---------------------------------------------------------------------------
# set_geo_batch
# ---------------------------------------------------------------------------


async def test_set_geo_batch_writes_one_set_per_item_with_ttl() -> None:
    redis_mock, pipe = _make_mock_redis()
    cache = PlacesCache(redis_mock)
    geo_a = _make_geo(lat=1.0)
    geo_b = _make_geo(lat=2.0)

    await cache.set_geo_batch({"google:a": geo_a, "google:b": geo_b})

    assert pipe.set.call_count == 2
    ttl = _expected_ttl()
    for call, (pid, expected) in zip(
        pipe.set.call_args_list,
        [("google:a", geo_a), ("google:b", geo_b)],
        strict=False,
    ):
        key, value = call.args
        kwargs = call.kwargs
        assert key == f"places:geo:{pid}"
        assert value == expected.model_dump_json()
        assert kwargs.get("ex") == ttl
    pipe.execute.assert_awaited_once()


async def test_set_geo_batch_empty_input_returns_without_redis_call() -> None:
    redis_mock, pipe = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    await cache.set_geo_batch({})

    pipe.execute.assert_not_called()
    redis_mock.pipeline.assert_not_called()


async def test_set_geo_batch_swallows_redis_error() -> None:
    redis_mock, pipe = _make_mock_redis()
    pipe.execute = AsyncMock(side_effect=RedisError("down"))
    cache = PlacesCache(redis_mock)

    # Must not raise.
    await cache.set_geo_batch({"google:a": _make_geo()})


# ---------------------------------------------------------------------------
# get_enrichment_batch
# ---------------------------------------------------------------------------


async def test_get_enrichment_batch_all_hit_returns_model_per_key() -> None:
    redis_mock, _ = _make_mock_redis()
    enr_a = _make_enrichment(rating=4.1)
    enr_b = _make_enrichment(rating=4.7)
    redis_mock.mget = AsyncMock(
        return_value=[enr_a.model_dump_json(), enr_b.model_dump_json()]
    )
    cache = PlacesCache(redis_mock)

    result = await cache.get_enrichment_batch(["google:a", "google:b"])

    redis_mock.mget.assert_awaited_once_with(
        ["places:enrichment:google:a", "places:enrichment:google:b"]
    )
    assert result["google:a"] is not None and result["google:a"].rating == 4.1
    assert result["google:b"] is not None and result["google:b"].rating == 4.7


async def test_get_enrichment_batch_partial_miss_returns_none() -> None:
    redis_mock, _ = _make_mock_redis()
    enr_a = _make_enrichment()
    redis_mock.mget = AsyncMock(return_value=[enr_a.model_dump_json(), None])
    cache = PlacesCache(redis_mock)

    result = await cache.get_enrichment_batch(["google:a", "google:b"])

    assert result["google:a"] is not None
    assert result["google:b"] is None


async def test_get_enrichment_batch_empty_input_short_circuits() -> None:
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    result = await cache.get_enrichment_batch([])

    assert result == {}
    redis_mock.mget.assert_not_called()


# ---------------------------------------------------------------------------
# set_enrichment_batch
# ---------------------------------------------------------------------------


async def test_set_enrichment_batch_writes_one_set_per_item_with_ttl() -> None:
    redis_mock, pipe = _make_mock_redis()
    cache = PlacesCache(redis_mock)
    enr_a = _make_enrichment(rating=4.1)
    enr_b = _make_enrichment(rating=4.7)

    await cache.set_enrichment_batch({"google:a": enr_a, "google:b": enr_b})

    assert pipe.set.call_count == 2
    ttl = _expected_ttl()
    for call in pipe.set.call_args_list:
        assert call.args[0].startswith("places:enrichment:")
        assert call.kwargs.get("ex") == ttl
    pipe.execute.assert_awaited_once()


async def test_set_enrichment_batch_empty_input_returns_without_redis_call() -> None:
    redis_mock, pipe = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    await cache.set_enrichment_batch({})

    pipe.execute.assert_not_called()
    redis_mock.pipeline.assert_not_called()


async def test_set_enrichment_batch_swallows_redis_error() -> None:
    redis_mock, pipe = _make_mock_redis()
    pipe.execute = AsyncMock(side_effect=RedisError("boom"))
    cache = PlacesCache(redis_mock)

    # Must not raise.
    await cache.set_enrichment_batch({"google:a": _make_enrichment()})


# ---------------------------------------------------------------------------
# HoursDict round-trip + programmer-error guard
# ---------------------------------------------------------------------------


async def test_hours_timezone_survives_json_roundtrip() -> None:
    """Writing then reading an enrichment preserves the IANA timezone string."""
    captured: dict[str, Any] = {}

    redis_mock, pipe = _make_mock_redis()

    def capture_set(key: str, value: str, ex: int) -> Any:
        captured[key] = value
        return pipe

    pipe.set = MagicMock(side_effect=capture_set)
    cache = PlacesCache(redis_mock)

    enr = _make_enrichment()
    await cache.set_enrichment_batch({"google:z": enr})

    # Now read it back via a new cache that returns the captured JSON.
    redis_mock.mget = AsyncMock(return_value=[captured["places:enrichment:google:z"]])
    result = await cache.get_enrichment_batch(["google:z"])

    loaded = result["google:z"]
    assert loaded is not None
    assert loaded.hours is not None
    assert loaded.hours.get("timezone") == "Asia/Bangkok"
    assert loaded.hours.get("monday") == "09:00-18:00"


async def test_set_enrichment_batch_raises_when_hours_has_days_without_timezone() -> (
    None
):
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    # Pydantic's HoursDict TypedDict allows this shape, but the cache
    # guards against the programmer error of forgetting the timezone.
    broken = PlaceEnrichment(
        hours={"monday": "09:00-18:00"},  # type: ignore[typeddict-item]
        fetched_at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="timezone"):
        await cache.set_enrichment_batch({"google:bad": broken})


# ---------------------------------------------------------------------------
# Reverse-geocode label cache
# ---------------------------------------------------------------------------


async def test_set_and_get_location_label_round_trip() -> None:
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)

    captured: dict[str, Any] = {}

    async def capture_set(key: str, value: str, ex: int) -> None:
        captured["key"] = key
        captured["value"] = value
        captured["ex"] = ex

    redis_mock.set = AsyncMock(side_effect=capture_set)
    await cache.set_location_label("52.1,11.6", "Magdeburg, Germany")

    assert captured["key"] == "places:geocode:52.1,11.6"
    assert captured["value"] == "Magdeburg, Germany"
    assert captured["ex"] == get_config().places.cache_ttl_days * 86400

    redis_mock.get = AsyncMock(return_value=b"Magdeburg, Germany")
    result = await cache.get_location_label("52.1,11.6")
    assert result == "Magdeburg, Germany"


async def test_get_location_label_returns_none_on_miss() -> None:
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)
    redis_mock.get = AsyncMock(return_value=None)

    assert await cache.get_location_label("0.0,0.0") is None


async def test_get_location_label_returns_none_on_redis_error() -> None:
    """Redis blips degrade gracefully — caller sees None, not an exception."""
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)
    redis_mock.get = AsyncMock(side_effect=RedisError("blip"))

    assert await cache.get_location_label("52.1,11.6") is None


async def test_set_location_label_swallows_redis_error() -> None:
    """Cache writes are best-effort — a blip must not break the request."""
    redis_mock, _ = _make_mock_redis()
    cache = PlacesCache(redis_mock)
    redis_mock.set = AsyncMock(side_effect=RedisError("blip"))

    await cache.set_location_label("52.1,11.6", "Magdeburg, Germany")  # no raise
