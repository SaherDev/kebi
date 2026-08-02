"""Tests for CachedGeocoder — the reverse-geocode bucket cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.providers.geocoding import CachedGeocoder, GeocodeResult

_RESULT = GeocodeResult(
    lat=13.7501, lng=100.5013, country="Thailand", city="Bangkok", country_code="th"
)


def _cached(
    inner_result: GeocodeResult | None,
    cached_raw: str | None = None,
) -> tuple[CachedGeocoder, MagicMock, MagicMock]:
    inner = MagicMock()
    inner.reverse = AsyncMock(return_value=inner_result)
    inner.search_area = AsyncMock(return_value=None)
    inner.geocode_place_id = AsyncMock(return_value=None)
    redis = MagicMock()
    redis.get = AsyncMock(return_value=cached_raw)
    redis.set = AsyncMock()
    return CachedGeocoder(inner, redis), inner, redis


async def test_miss_calls_inner_and_writes_back() -> None:
    cached, inner, redis = _cached(_RESULT)
    result = await cached.reverse(lat=13.7501, lng=100.5013)
    assert result == _RESULT
    inner.reverse.assert_awaited_once()
    redis.set.assert_awaited_once()
    key = redis.set.await_args.args[0]
    # 3-decimal bucket (~110 m).
    assert key == "geo:rev:13.75:100.501"


async def test_hit_skips_inner_and_echoes_exact_coords() -> None:
    cached_json = _RESULT.model_dump_json()
    cached, inner, _ = _cached(None, cached_raw=cached_json)
    # A slightly different point in the same bucket.
    result = await cached.reverse(lat=13.7503, lng=100.5011)
    assert result is not None
    assert result.city == "Bangkok"
    # The bucket's names are shared; the caller's GPS stays canonical.
    assert result.lat == 13.7503 and result.lng == 100.5011
    inner.reverse.assert_not_awaited()


async def test_null_marker_caches_misses() -> None:
    cached, inner, redis = _cached(None)
    assert await cached.reverse(lat=0.0, lng=0.0) is None
    # Miss stored as marker with the short TTL.
    assert redis.set.await_args.args[1] == "null"

    cached2, inner2, _ = _cached(_RESULT, cached_raw="null")
    assert await cached2.reverse(lat=0.0, lng=0.0) is None
    inner2.reverse.assert_not_awaited()


async def test_redis_error_fails_open_to_inner() -> None:
    cached, inner, redis = _cached(_RESULT)
    redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
    result = await cached.reverse(lat=13.75, lng=100.5)
    assert result == _RESULT
    inner.reverse.assert_awaited_once()


async def test_ttl_capped_at_provider_terms() -> None:
    inner = MagicMock()
    inner.reverse = AsyncMock(return_value=_RESULT)
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    # Config asks for a year — the cache clamps to the 30-day ToS cap.
    cached = CachedGeocoder(inner, redis, ttl_seconds=365 * 24 * 60 * 60)
    await cached.reverse(lat=1.0, lng=1.0)
    assert redis.set.await_args.kwargs["ex"] == 30 * 24 * 60 * 60


async def test_forward_and_place_id_pass_through_uncached() -> None:
    cached, inner, redis = _cached(None)
    await cached.search_area(query="Hoi An", region_code="vn")
    await cached.geocode_place_id("google:x")
    inner.search_area.assert_awaited_once_with(query="Hoi An", region_code="vn")
    inner.geocode_place_id.assert_awaited_once_with("google:x")
    redis.get.assert_not_awaited()
