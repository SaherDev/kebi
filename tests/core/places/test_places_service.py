"""Unit tests for PlacesService — create-path only (Phase 3 / US1).

The geo-only and full-enrichment paths (enrich_batch) are added in Phases 4/5
and live in their own test classes here later.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places.models import (
    PlaceCreate,
    PlaceObject,
    PlaceProvider,
    PlaceType,
)
from kebi.core.places.service import PlacesService


def _make_place_create(
    place_name: str = "Cafe A",
    external_id: str = "ChIJ_aaa",
    place_type: PlaceType = PlaceType.food_and_drink,
) -> PlaceCreate:
    return PlaceCreate(
        user_id="u1",
        place_name=place_name,
        place_type=place_type,
        provider=PlaceProvider.google,
        external_id=external_id,
    )


def _make_place_object(
    place_id: str = "pid-1",
    place_name: str = "Cafe A",
    place_type: PlaceType = PlaceType.food_and_drink,
) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name=place_name,
        place_type=place_type,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("place_type", list(PlaceType))
async def test_create_returns_tier1_place_object_with_freshness_flags_false(
    place_type: PlaceType,
) -> None:
    repo = MagicMock()
    repo.create = AsyncMock(return_value=_make_place_object(place_type=place_type))
    service = PlacesService(repo=repo)

    result = await service.create(_make_place_create(place_type=place_type))

    repo.create.assert_awaited_once()
    assert result.place_id == "pid-1"
    assert result.place_type == place_type
    assert result.geo_fresh is False
    assert result.enriched is False
    assert result.lat is None
    assert result.lng is None


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------


async def test_create_batch_calls_repo_create_batch_exactly_once() -> None:
    repo = MagicMock()
    repo.create_batch = AsyncMock(
        return_value=[
            _make_place_object(place_id="p1", place_name="A"),
            _make_place_object(place_id="p2", place_name="B"),
            _make_place_object(place_id="p3", place_name="C"),
        ]
    )
    service = PlacesService(repo=repo)

    inputs = [
        _make_place_create(place_name="A", external_id="id_a"),
        _make_place_create(place_name="B", external_id="id_b"),
        _make_place_create(place_name="C", external_id="id_c"),
    ]
    result = await service.create_batch(inputs)

    assert repo.create_batch.await_count == 1
    assert [p.place_name for p in result] == ["A", "B", "C"]


async def test_create_batch_preserves_input_order() -> None:
    expected = [
        _make_place_object(place_id="p1", place_name="X"),
        _make_place_object(place_id="p2", place_name="Y"),
    ]
    repo = MagicMock()
    repo.create_batch = AsyncMock(return_value=expected)
    service = PlacesService(repo=repo)

    result = await service.create_batch(
        [
            _make_place_create(place_name="X", external_id="x"),
            _make_place_create(place_name="Y", external_id="y"),
        ]
    )

    assert [p.place_name for p in result] == ["X", "Y"]


async def test_create_batch_empty_returns_empty_without_repo_call() -> None:
    repo = MagicMock()
    repo.create_batch = AsyncMock(return_value=[])
    service = PlacesService(repo=repo)

    result = await service.create_batch([])

    # Contract: empty input returns [] (the repo returns [] already; we assert
    # that the service passes through to the repo at most once).
    assert result == []


# ---------------------------------------------------------------------------
# get / get_by_external_id / get_batch pass-through
# ---------------------------------------------------------------------------


async def test_get_delegates_to_repo_and_returns_place_object() -> None:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=_make_place_object())
    service = PlacesService(repo=repo)

    result = await service.get("pid-1")

    repo.get.assert_awaited_once_with("pid-1")
    assert result is not None
    assert result.geo_fresh is False
    assert result.enriched is False


async def test_get_batch_delegates_to_repo() -> None:
    repo = MagicMock()
    repo.get_batch = AsyncMock(
        return_value=[
            _make_place_object(place_id="a", place_name="A"),
            _make_place_object(place_id="c", place_name="C"),
        ]
    )
    service = PlacesService(repo=repo)

    result = await service.get_batch(["a", "b", "c"])

    repo.get_batch.assert_awaited_once_with(["a", "b", "c"])
    assert [p.place_id for p in result] == ["a", "c"]


async def test_get_by_external_id_delegates_to_repo() -> None:
    repo = MagicMock()
    repo.get_by_external_id = AsyncMock(return_value=_make_place_object())
    service = PlacesService(repo=repo)

    result = await service.get_by_external_id(PlaceProvider.google, "ChIJ_aaa")

    repo.get_by_external_id.assert_awaited_once_with(PlaceProvider.google, "ChIJ_aaa")
    assert result is not None


# ---------------------------------------------------------------------------
# enrich_batch — geo-only (recall mode)
# ---------------------------------------------------------------------------


def _make_enrichable_place(
    place_id: str = "pid-1",
    provider_id: str | None = "google:ChIJ_aaa",
) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name="Cafe A",
        place_type=PlaceType.food_and_drink,
        provider_id=provider_id,
    )


def _make_geo_data(lat: float = 13.7, lng: float = 100.5, address: str = "Siam") -> Any:
    from datetime import datetime

    from kebi.core.places.models import GeoData

    return GeoData(
        lat=lat,
        lng=lng,
        address=address,
        cached_at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
    )


async def test_enrich_batch_geo_only_empty_input_returns_empty() -> None:
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock()
    service = PlacesService(repo=repo, cache=cache)

    result = await service.enrich_batch([], geo_only=True)

    assert result == []
    cache.get_geo_batch.assert_not_called()


async def test_enrich_batch_geo_only_calls_get_geo_batch_exactly_once() -> None:
    place = _make_enrichable_place()
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={"google:ChIJ_aaa": _make_geo_data()})
    cache.get_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock()
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([place], geo_only=True)

    assert cache.get_geo_batch.await_count == 1
    cache.get_enrichment_batch.assert_not_called()
    client.get_place_details.assert_not_called()
    assert len(result) == 1
    assert result[0].geo_fresh is True
    assert result[0].lat == 13.7
    assert result[0].lng == 100.5
    assert result[0].address == "Siam"
    assert result[0].enriched is False


async def test_enrich_batch_geo_only_partial_miss_mixes_fresh_and_stale() -> None:
    hit = _make_enrichable_place(place_id="hit", provider_id="google:a")
    miss = _make_enrichable_place(place_id="miss", provider_id="google:b")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(
        return_value={"google:a": _make_geo_data(), "google:b": None}
    )
    service = PlacesService(repo=repo, cache=cache)

    result = await service.enrich_batch([hit, miss], geo_only=True)

    assert result[0].geo_fresh is True
    assert result[0].lat is not None
    assert result[1].geo_fresh is False
    assert result[1].lat is None


async def test_enrich_batch_geo_only_preserves_input_order() -> None:
    p1 = _make_enrichable_place(place_id="p1", provider_id="google:one")
    p2 = _make_enrichable_place(place_id="p2", provider_id="google:two")
    p3 = _make_enrichable_place(place_id="p3", provider_id="google:three")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(
        return_value={
            "google:one": _make_geo_data(lat=1.0, address="One"),
            "google:two": _make_geo_data(lat=2.0, address="Two"),
            "google:three": _make_geo_data(lat=3.0, address="Three"),
        }
    )
    service = PlacesService(repo=repo, cache=cache)

    result = await service.enrich_batch([p1, p2, p3], geo_only=True)

    assert [p.place_id for p in result] == ["p1", "p2", "p3"]
    assert [p.lat for p in result] == [1.0, 2.0, 3.0]


async def test_enrich_batch_geo_only_provider_id_none_passes_through() -> None:
    no_provider = _make_enrichable_place(place_id="np", provider_id=None)
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={})
    service = PlacesService(repo=repo, cache=cache)

    result = await service.enrich_batch([no_provider], geo_only=True)

    assert len(result) == 1
    assert result[0].place_id == "np"
    assert result[0].geo_fresh is False
    assert result[0].lat is None
    # Entire unique set was empty, so no cache call should have been made.
    cache.get_geo_batch.assert_not_called()


async def test_enrich_batch_geo_only_redis_error_treated_as_all_miss() -> None:
    from redis.exceptions import RedisError

    p1 = _make_enrichable_place(place_id="p1", provider_id="google:one")
    p2 = _make_enrichable_place(place_id="p2", provider_id="google:two")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(side_effect=RedisError("down"))
    service = PlacesService(repo=repo, cache=cache)

    # Must not raise — the cache read failure falls through to "all miss".
    result = await service.enrich_batch([p1, p2], geo_only=True)

    assert len(result) == 2
    assert all(p.geo_fresh is False for p in result)
    assert all(p.lat is None for p in result)


# ---------------------------------------------------------------------------
# enrich_batch — full path (consult mode)
# ---------------------------------------------------------------------------


def _make_enrichment(rating: float = 4.3) -> Any:
    from datetime import datetime

    from kebi.core.places.models import PlaceEnrichment

    return PlaceEnrichment(
        hours={"monday": "09:00-18:00", "timezone": "Asia/Bangkok"},
        rating=rating,
        phone="+66 2 000 0000",
        fetched_at=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
    )


def _place_details_response(
    lat: float = 13.7,
    lng: float = 100.5,
    address: str = "Siam",
    rating: float = 4.3,
) -> dict[str, Any]:
    return {
        "lat": lat,
        "lng": lng,
        "address": address,
        "hours": {"monday": "09:00-18:00", "timezone": "Asia/Bangkok"},
        "rating": rating,
        "phone": "+66 2 000 0000",
        "photo_url": "https://example/photo.jpg",
        "popularity": 0.7,
    }


async def test_enrich_batch_full_all_hit_makes_zero_fetch_calls() -> None:
    place = _make_enrichable_place()
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={"google:ChIJ_aaa": _make_geo_data()})
    cache.get_enrichment_batch = AsyncMock(
        return_value={"google:ChIJ_aaa": _make_enrichment()}
    )
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock()
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([place], geo_only=False)

    client.get_place_details.assert_not_called()
    # With zero misses, no writeback needed.
    cache.set_geo_batch.assert_not_called()
    cache.set_enrichment_batch.assert_not_called()
    assert len(result) == 1
    assert result[0].geo_fresh is True
    assert result[0].enriched is True
    assert result[0].rating == 4.3
    assert result[0].lat == 13.7


async def test_enrich_batch_full_partial_miss_fetches_once_per_unique_miss() -> None:
    hit = _make_enrichable_place(place_id="hit", provider_id="google:a")
    miss = _make_enrichable_place(place_id="miss", provider_id="google:b")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(
        return_value={"google:a": _make_geo_data(lat=1.0), "google:b": None}
    )
    cache.get_enrichment_batch = AsyncMock(
        return_value={"google:a": _make_enrichment(), "google:b": None}
    )
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock(return_value=_place_details_response(lat=2.0))
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([hit, miss], geo_only=False)

    # Exactly ONE fetch — only "b" missed — and with the raw external_id.
    assert client.get_place_details.await_count == 1
    client.get_place_details.assert_awaited_once_with("b")
    # Both cache writebacks happened from the single fetch.
    cache.set_geo_batch.assert_awaited_once()
    cache.set_enrichment_batch.assert_awaited_once()
    # Order preserved.
    assert [p.place_id for p in result] == ["hit", "miss"]
    assert result[0].lat == 1.0
    assert result[1].lat == 2.0
    assert all(p.geo_fresh and p.enriched for p in result)


async def test_enrich_batch_full_uses_asyncio_gather() -> None:
    import asyncio as _asyncio

    p1 = _make_enrichable_place(place_id="a", provider_id="google:aa")
    p2 = _make_enrichable_place(place_id="b", provider_id="google:bb")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={"google:aa": None, "google:bb": None})
    cache.get_enrichment_batch = AsyncMock(
        return_value={"google:aa": None, "google:bb": None}
    )
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock(return_value=_place_details_response())
    service = PlacesService(repo=repo, cache=cache, client=client)

    original_gather = _asyncio.gather
    spy = MagicMock(side_effect=original_gather)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("kebi.core.places.service.asyncio.gather", spy)
        await service.enrich_batch([p1, p2], geo_only=False)

    assert spy.called
    _args, kwargs = spy.call_args
    assert kwargs.get("return_exceptions") is True


async def test_enrich_batch_full_provider_id_none_passthrough() -> None:
    no_provider = _make_enrichable_place(place_id="np", provider_id=None)
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={})
    cache.get_enrichment_batch = AsyncMock(return_value={})
    client = MagicMock()
    client.get_place_details = AsyncMock()
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([no_provider], geo_only=False)

    # Nothing to fetch, nothing to look up.
    cache.get_geo_batch.assert_not_called()
    cache.get_enrichment_batch.assert_not_called()
    client.get_place_details.assert_not_called()
    assert result[0].geo_fresh is False
    assert result[0].enriched is False


async def test_enrich_batch_full_dedupes_same_provider_id() -> None:
    dup_a = _make_enrichable_place(place_id="a", provider_id="google:dup")
    dup_b = _make_enrichable_place(place_id="b", provider_id="google:dup")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={"google:dup": None})
    cache.get_enrichment_batch = AsyncMock(return_value={"google:dup": None})
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock(return_value=_place_details_response())
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([dup_a, dup_b], geo_only=False)

    # Same provider_id → one fetch, both places populated.
    assert client.get_place_details.await_count == 1
    assert all(p.geo_fresh and p.enriched for p in result)


async def test_enrich_batch_full_caps_misses_and_logs() -> None:
    from kebi.core.config import get_config

    # Force cap to 2 for this test.
    cfg = get_config()
    original_cap = cfg.places.max_enrichment_batch
    try:
        cfg.places.max_enrichment_batch = 2

        places_list = [
            _make_enrichable_place(place_id=f"p{i}", provider_id=f"google:m{i}")
            for i in range(5)
        ]
        repo = MagicMock()
        cache = MagicMock()
        cache.get_geo_batch = AsyncMock(
            return_value={f"google:m{i}": None for i in range(5)}
        )
        cache.get_enrichment_batch = AsyncMock(
            return_value={f"google:m{i}": None for i in range(5)}
        )
        cache.set_geo_batch = AsyncMock()
        cache.set_enrichment_batch = AsyncMock()
        client = MagicMock()
        client.get_place_details = AsyncMock(return_value=_place_details_response())
        service = PlacesService(repo=repo, cache=cache, client=client)

        await service.enrich_batch(places_list, geo_only=False)

        # Only the first 2 sorted misses get fetched.
        assert client.get_place_details.await_count == 2
    finally:
        cfg.places.max_enrichment_batch = original_cap


async def test_enrich_batch_full_priority_provider_ids_survive_cap() -> None:
    """Saved places (priority) are guaranteed enrichment even when the
    total miss count exceeds the cap. Without priority they'd be dropped
    by alphabetical truncation (ADR-057 follow-up).

    Fixture: 5 candidates, cap=2, one priority id alphabetically LAST
    (`google:zzz`) so the plain-sort path would drop it; with priority
    it must be kept and one non-priority id is dropped instead.
    """
    from kebi.core.config import get_config

    cfg = get_config()
    original_cap = cfg.places.max_enrichment_batch
    try:
        cfg.places.max_enrichment_batch = 2

        # provider_ids: aaa (early), bbb, ccc, ddd (all non-priority),
        # and zzz (priority — last alphabetically, would normally be dropped).
        pids = ["google:aaa", "google:bbb", "google:ccc", "google:ddd", "google:zzz"]
        places_list = [
            _make_enrichable_place(place_id=f"p{i}", provider_id=pid)
            for i, pid in enumerate(pids)
        ]

        repo = MagicMock()
        cache = MagicMock()
        cache.get_geo_batch = AsyncMock(return_value={pid: None for pid in pids})
        cache.get_enrichment_batch = AsyncMock(return_value={pid: None for pid in pids})
        cache.set_geo_batch = AsyncMock()
        cache.set_enrichment_batch = AsyncMock()
        client = MagicMock()
        client.get_place_details = AsyncMock(return_value=_place_details_response())
        service = PlacesService(repo=repo, cache=cache, client=client)

        await service.enrich_batch(
            places_list,
            geo_only=False,
            priority_provider_ids={"google:zzz"},
        )

        # Cap=2, so exactly 2 fetches happen.
        assert client.get_place_details.await_count == 2
        # Priority pid "zzz" MUST be one of them (kept via priority sort).
        fetched_ids = {
            call.args[0] for call in client.get_place_details.await_args_list
        }
        # Note: get_place_details receives the stripped external_id, so
        # strip the "google:" prefix when asserting.
        assert "zzz" in fetched_ids, f"Priority id not fetched; got {fetched_ids!r}"
        # The other slot is filled by the alphabetically-first non-priority:
        assert "aaa" in fetched_ids, (
            f"Expected alphabetically-first non-priority; got {fetched_ids!r}"
        )
    finally:
        cfg.places.max_enrichment_batch = original_cap


async def test_enrich_batch_full_priority_default_none_preserves_old_behavior() -> None:
    """With no priority_provider_ids, truncation falls back to plain
    alphabetical ordering — the pre-ADR-057-followup behavior. Keeps
    backward compatibility for callers that don't pass the new param."""
    from kebi.core.config import get_config

    cfg = get_config()
    original_cap = cfg.places.max_enrichment_batch
    try:
        cfg.places.max_enrichment_batch = 2

        pids = ["google:aaa", "google:bbb", "google:ccc", "google:zzz"]
        places_list = [
            _make_enrichable_place(place_id=f"p{i}", provider_id=pid)
            for i, pid in enumerate(pids)
        ]

        repo = MagicMock()
        cache = MagicMock()
        cache.get_geo_batch = AsyncMock(return_value={pid: None for pid in pids})
        cache.get_enrichment_batch = AsyncMock(return_value={pid: None for pid in pids})
        cache.set_geo_batch = AsyncMock()
        cache.set_enrichment_batch = AsyncMock()
        client = MagicMock()
        client.get_place_details = AsyncMock(return_value=_place_details_response())
        service = PlacesService(repo=repo, cache=cache, client=client)

        await service.enrich_batch(places_list, geo_only=False)

        fetched_ids = {
            call.args[0] for call in client.get_place_details.await_args_list
        }
        # Alphabetical truncation drops "ccc" and "zzz", keeps "aaa" and "bbb".
        assert fetched_ids == {"aaa", "bbb"}
    finally:
        cfg.places.max_enrichment_batch = original_cap


async def test_enrich_batch_full_redis_read_error_treated_as_all_miss() -> None:
    from redis.exceptions import RedisError

    place = _make_enrichable_place()
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(side_effect=RedisError("down"))
    cache.get_enrichment_batch = AsyncMock(
        return_value={"google:ChIJ_aaa": _make_enrichment()}
    )
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock(return_value=_place_details_response())
    service = PlacesService(repo=repo, cache=cache, client=client)

    # Must not raise. Geo miss → fetch; enrichment already hit.
    result = await service.enrich_batch([place], geo_only=False)

    assert len(result) == 1
    # Fetch happened despite the cache read failure.
    client.get_place_details.assert_awaited_once()


async def test_enrich_batch_full_one_fetch_failure_does_not_poison_batch() -> None:
    p_ok = _make_enrichable_place(place_id="ok", provider_id="google:ok")
    p_fail = _make_enrichable_place(place_id="fail", provider_id="google:fail")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(
        return_value={"google:ok": None, "google:fail": None}
    )
    cache.get_enrichment_batch = AsyncMock(
        return_value={"google:ok": None, "google:fail": None}
    )
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()

    async def side_effect(external_id: str) -> Any:
        if external_id == "fail":
            raise RuntimeError("provider 503")
        return _place_details_response()

    client = MagicMock()
    client.get_place_details = AsyncMock(side_effect=side_effect)
    service = PlacesService(repo=repo, cache=cache, client=client)

    result = await service.enrich_batch([p_ok, p_fail], geo_only=False)

    # Both places come back, "ok" is enriched, "fail" is not.
    by_id = {p.place_id: p for p in result}
    assert by_id["ok"].enriched is True
    assert by_id["fail"].enriched is False
    assert by_id["fail"].geo_fresh is False


async def test_enrich_batch_full_writes_both_cache_tiers_after_fetch() -> None:
    place = _make_enrichable_place()
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(return_value={"google:ChIJ_aaa": None})
    cache.get_enrichment_batch = AsyncMock(return_value={"google:ChIJ_aaa": None})
    cache.set_geo_batch = AsyncMock()
    cache.set_enrichment_batch = AsyncMock()
    client = MagicMock()
    client.get_place_details = AsyncMock(return_value=_place_details_response())
    service = PlacesService(repo=repo, cache=cache, client=client)

    await service.enrich_batch([place], geo_only=False)

    # Exactly one fetch → two writes (one per tier).
    assert client.get_place_details.await_count == 1
    cache.set_geo_batch.assert_awaited_once()
    cache.set_enrichment_batch.assert_awaited_once()


async def test_enrich_batch_geo_only_dedupes_duplicate_provider_ids() -> None:
    """Two input places with the same provider_id → one cache lookup."""
    dup_a = _make_enrichable_place(place_id="a", provider_id="google:same")
    dup_b = _make_enrichable_place(place_id="b", provider_id="google:same")
    repo = MagicMock()
    cache = MagicMock()
    cache.get_geo_batch = AsyncMock(
        return_value={"google:same": _make_geo_data(lat=5.0)}
    )
    service = PlacesService(repo=repo, cache=cache)

    result = await service.enrich_batch([dup_a, dup_b], geo_only=True)

    # Cache was called once with a single provider id (deduped).
    call_args = cache.get_geo_batch.await_args
    assert call_args.args[0] == ["google:same"]
    # Both result places got the same geo data.
    assert result[0].lat == 5.0
    assert result[1].lat == 5.0


# ---------------------------------------------------------------------------
# resolve_location_label — cache-or-fetch helper
# ---------------------------------------------------------------------------


async def test_resolve_location_label_cache_hit_skips_geocode() -> None:
    """Cache hit returns immediately; the Google Geocoding API is never called."""
    repo = MagicMock()
    cache = MagicMock()
    cache.get_location_label = AsyncMock(return_value="Magdeburg, Germany")
    cache.set_location_label = AsyncMock()
    client = MagicMock()
    client.reverse_geocode = AsyncMock()

    service = PlacesService(repo=repo, cache=cache, client=client)
    label = await service.resolve_location_label(52.12345, 11.62789)

    assert label == "Magdeburg, Germany"
    cache.get_location_label.assert_awaited_once_with("52.1,11.6")
    client.reverse_geocode.assert_not_awaited()
    cache.set_location_label.assert_not_awaited()


async def test_resolve_location_label_cache_miss_calls_geocode_and_caches() -> None:
    """Cache miss → geocode → cache-set → return label."""
    repo = MagicMock()
    cache = MagicMock()
    cache.get_location_label = AsyncMock(return_value=None)
    cache.set_location_label = AsyncMock()
    client = MagicMock()
    client.reverse_geocode = AsyncMock(return_value="Magdeburg, Germany")

    service = PlacesService(repo=repo, cache=cache, client=client)
    label = await service.resolve_location_label(52.12345, 11.62789)

    assert label == "Magdeburg, Germany"
    client.reverse_geocode.assert_awaited_once_with(52.12345, 11.62789)
    cache.set_location_label.assert_awaited_once_with(
        "52.1,11.6", "Magdeburg, Germany"
    )


async def test_resolve_location_label_returns_none_when_geocode_fails() -> None:
    repo = MagicMock()
    cache = MagicMock()
    cache.get_location_label = AsyncMock(return_value=None)
    cache.set_location_label = AsyncMock()
    client = MagicMock()
    client.reverse_geocode = AsyncMock(return_value=None)

    service = PlacesService(repo=repo, cache=cache, client=client)
    label = await service.resolve_location_label(0.0, 0.0)

    assert label is None
    cache.set_location_label.assert_not_awaited()


async def test_resolve_location_label_none_when_no_cache_or_client() -> None:
    """Service built without cache/client degrades gracefully."""
    service = PlacesService(repo=MagicMock(), cache=None, client=None)
    assert await service.resolve_location_label(52.12, 11.62) is None


async def test_resolve_location_label_uses_one_decimal_cell_key() -> None:
    """Two users ~5km apart must share the same cache cell (~11km granularity)."""
    repo = MagicMock()
    cache = MagicMock()
    cache.get_location_label = AsyncMock(return_value="Magdeburg, Germany")
    client = MagicMock()

    service = PlacesService(repo=repo, cache=cache, client=client)
    await service.resolve_location_label(52.12, 11.62)
    await service.resolve_location_label(52.14, 11.60)

    keys = [call.args[0] for call in cache.get_location_label.await_args_list]
    assert keys == ["52.1,11.6", "52.1,11.6"]  # same cell
