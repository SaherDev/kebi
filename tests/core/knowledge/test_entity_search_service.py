"""Tests for EntitySearchService — the anchor-chip typeahead backend."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.areas.models import AreaProfile
from kebi.core.knowledge.entity_search_service import EntitySearchService
from kebi.core.knowledge.schemas import ResolvedGeo
from kebi.core.places.models import HybridSearchHit, PlaceCore


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ttl))


def _profile(geo_key: str, name: str, level: str = "neighbourhood") -> AreaProfile:
    return AreaProfile(geo_key=geo_key, name=name, level=level, summary="s")


def _hit(place_id: str, name: str) -> HybridSearchHit:
    return HybridSearchHit(
        place=PlaceCore(id=place_id, place_name=name),
        rrf_score=1.0,
        vector_rank=1,
        text_rank=1,
    )


def _service(
    *,
    area_profiles: list[AreaProfile] | None = None,
    place_hits: list[HybridSearchHit] | None = None,
    resolve_country: ResolvedGeo | None = None,
    resolve_city: ResolvedGeo | None = None,
    resolve_city_global: ResolvedGeo | None = None,
    cache: _FakeCache | None = None,
) -> tuple[EntitySearchService, AsyncMock, AsyncMock]:
    area_repo = AsyncMock()
    area_repo.search_by_name = AsyncMock(return_value=area_profiles or [])
    hybrid = AsyncMock()
    hybrid.search = AsyncMock(return_value=place_hits or [])
    geo = AsyncMock()
    geo.resolve_country = AsyncMock(return_value=resolve_country)
    geo.resolve_city = AsyncMock(return_value=resolve_city)
    geo.resolve_city_global = AsyncMock(return_value=resolve_city_global)
    svc = EntitySearchService(
        area_repo=area_repo,
        hybrid_search=hybrid,
        geo_resolver=geo,
        cache=cache,
        cache_ttl_seconds=100,
        area_limit=3,
    )
    return svc, geo, hybrid


async def test_known_areas_lead_and_skip_resolver() -> None:
    svc, geo, _ = _service(
        area_profiles=[_profile("id/bali/canggu", "Canggu")],
        place_hits=[_hit("p1", "Canggu Coffee Lab")],
    )
    out = await svc.search("cang", limit=8)
    assert [a.name for a in out.areas] == ["Canggu"]
    assert out.areas[0].geo_key == "id/bali/canggu"
    assert [p.id for p in out.places] == ["p1"]
    geo.resolve_country.assert_not_awaited()
    geo.resolve_city.assert_not_awaited()


async def test_places_searched_unscoped() -> None:
    svc, _, hybrid = _service(area_profiles=[_profile("id/bali", "Bali")])
    await svc.search("bali", limit=8)
    kwargs = hybrid.search.await_args.kwargs
    assert kwargs["user_id"] is None


async def test_bare_name_tries_country_first() -> None:
    svc, geo, _ = _service(resolve_country=ResolvedGeo(country_code="vn"))
    out = await svc.search("Vietnam", limit=8)
    assert len(out.areas) == 1
    assert out.areas[0].geo_key == "vn"
    assert out.areas[0].level == "country"
    # A verified country never falls through to a city named like one.
    geo.resolve_city_global.assert_not_awaited()
    geo.resolve_city.assert_not_awaited()


async def test_bare_city_name_resolves_via_global_lookup() -> None:
    # The Tokyo case: not a country, so the verified unconstrained city
    # lookup runs and mints an anchorable key.
    svc, geo, _ = _service(
        resolve_country=None,
        resolve_city_global=ResolvedGeo(country_code="jp", city="Tokyo"),
    )
    out = await svc.search("Tokyo", limit=8)
    assert len(out.areas) == 1
    assert out.areas[0].geo_key == "jp/tokyo"
    assert out.areas[0].level == "city"
    geo.resolve_city_global.assert_awaited_once_with("Tokyo")


async def test_name_comma_country_resolves_as_city() -> None:
    svc, geo, _ = _service(
        resolve_country=ResolvedGeo(country_code="id"),
        resolve_city=ResolvedGeo(country_code="id", city="Ubud"),
    )
    out = await svc.search("Ubud, Indonesia", limit=8)
    assert out.areas[0].geo_key == "id/ubud"
    assert out.areas[0].level == "city"
    geo.resolve_city.assert_awaited_once_with("Ubud", "id")


async def test_unverifiable_name_yields_no_area() -> None:
    svc, _, _ = _service(resolve_country=None)
    out = await svc.search("Atlantis", limit=8)
    assert out.areas == []


async def test_resolver_verdict_cached_hit_and_miss() -> None:
    cache = _FakeCache()
    svc, geo, _ = _service(resolve_country=ResolvedGeo(country_code="vn"), cache=cache)
    first = await svc.search("Vietnam", limit=8)
    second = await svc.search("Vietnam", limit=8)
    assert first.areas == second.areas
    geo.resolve_country.assert_awaited_once()  # second run served from cache

    # A miss is cached too — an unverifiable name geocodes once, not per key.
    svc2, geo2, _ = _service(resolve_country=None, cache=cache)
    await svc2.search("Atlantis", limit=8)
    await svc2.search("Atlantis", limit=8)
    geo2.resolve_country.assert_awaited_once()


async def test_cache_errors_degrade_to_live_resolution() -> None:
    broken = AsyncMock()
    broken.get = AsyncMock(side_effect=RuntimeError("redis down"))
    broken.set = AsyncMock(side_effect=RuntimeError("redis down"))
    svc, _, _ = _service(resolve_country=ResolvedGeo(country_code="vn"))
    svc._cache = broken
    out = await svc.search("Vietnam", limit=8)
    assert out.areas[0].geo_key == "vn"


async def test_area_rows_consume_place_budget() -> None:
    svc, _, hybrid = _service(area_profiles=[_profile("id/bali", "Bali")])
    await svc.search("bali", limit=3)
    assert hybrid.search.await_args.kwargs["limit"] == 2
