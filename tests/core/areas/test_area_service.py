"""Tests for AreaService — store-first, verified-or-refuse area resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from kebi.core.areas import AreaContext, AreaEntity, AreaService
from kebi.providers.geocoding import GeocodeResult


def _stored(
    key: str = "vn/hoi-an",
    name: str = "Hoi An",
    cc: str = "vn",
    entity_type: str = "city",
    refreshed_days_ago: float = 0,
    provider_id: str | None = "google:ChIJx",
) -> AreaEntity:
    return AreaEntity(
        entity_key=key,
        entity_type=entity_type,  # type: ignore[arg-type]
        name=name,
        country_code=cc,
        lat=15.88,
        lng=108.33,
        provider_id=provider_id,
        geo_refreshed_at=datetime.now(UTC) - timedelta(days=refreshed_days_ago),
    )


def _repo(
    get_result: AreaEntity | None = None,
    alias_result: AreaEntity | None = None,
) -> MagicMock:
    repo = MagicMock()
    repo.get = AsyncMock(return_value=get_result)
    repo.find_by_alias = AsyncMock(return_value=alias_result)
    repo.upsert = AsyncMock(side_effect=lambda e: e)
    repo.update_geometry = AsyncMock()
    return repo


def _geocoder(result: GeocodeResult | None = None) -> MagicMock:
    geocoder = MagicMock()
    geocoder.search_area = AsyncMock(return_value=result)
    geocoder.geocode_place_id = AsyncMock(return_value=None)
    geocoder.reverse = AsyncMock(return_value=None)
    return geocoder


def _city_result(
    name: str = "Hoi An",
    cc: str = "vn",
    place_type: str = "locality",
    city: str | None = None,
) -> GeocodeResult:
    return GeocodeResult(
        lat=15.88,
        lng=108.33,
        name=name,
        city=city if city is not None else name,
        country="Vietnam",
        country_code=cc,
        place_type=place_type,
        bbox=[15.8, 15.95, 108.2, 108.4],
        provider_id="google:ChIJx",
    )


# ---- resolve_city ----------------------------------------------------------


async def test_store_hit_makes_zero_geocoder_calls() -> None:
    repo = _repo(get_result=_stored())
    geocoder = _geocoder()
    svc = AreaService(repo, geocoder)
    entity = await svc.resolve_city("Hoi An", "vn")
    assert entity is not None and entity.entity_key == "vn/hoi-an"
    geocoder.search_area.assert_not_awaited()


async def test_diacritic_spelling_hits_the_same_key() -> None:
    repo = _repo(get_result=_stored())
    svc = AreaService(repo, _geocoder())
    entity = await svc.resolve_city("Hội An", "vn")
    assert entity is not None
    repo.get.assert_awaited_once_with("vn/hoi-an")


async def test_alias_hit_skips_geocoder() -> None:
    stored = _stored(key="vn/ho-chi-minh-city", name="Ho Chi Minh City")
    repo = _repo(get_result=None, alias_result=stored)
    geocoder = _geocoder()
    svc = AreaService(repo, geocoder)
    entity = await svc.resolve_city("Saigon", "vn")
    assert entity is not None and entity.name == "Ho Chi Minh City"
    repo.find_by_alias.assert_awaited_once_with("vn", "saigon")
    geocoder.search_area.assert_not_awaited()


async def test_miss_geocodes_verifies_and_persists() -> None:
    repo = _repo()
    svc = AreaService(repo, _geocoder(_city_result()))
    entity = await svc.resolve_city("Hoi An", "vn")
    assert entity is not None
    assert entity.entity_key == "vn/hoi-an"
    assert entity.bbox == [15.8, 15.95, 108.2, 108.4]
    assert entity.parent_key == "vn"
    assert entity.provider_id == "google:ChIJx"
    repo.upsert.assert_awaited_once()


async def test_asked_spelling_learned_as_alias_when_it_differs() -> None:
    result = _city_result(name="Ho Chi Minh City")
    repo = _repo()
    svc = AreaService(repo, _geocoder(result))
    entity = await svc.resolve_city("Saigon", "vn")
    # "Saigon" round-trips only if it matches — here it doesn't, so refuse.
    assert entity is None

    # A diacritic variant of the canonical name resolves and records the
    # asked slug only when it differs from the canonical slug.
    svc2 = AreaService(_repo(), _geocoder(_city_result(name="Hội An")))
    entity2 = await svc2.resolve_city("Hội An", "vn")
    assert entity2 is not None
    assert entity2.aliases == []  # same slug — no alias recorded


async def test_round_trip_failure_refuses_and_persists_nothing() -> None:
    # Top hit is a different city ("Paris" matching a feature in Huế).
    repo = _repo()
    svc = AreaService(repo, _geocoder(_city_result(name="Hue", city="Hue")))
    assert await svc.resolve_city("Paris", "vn") is None
    repo.upsert.assert_not_awaited()


async def test_wrong_country_refused() -> None:
    repo = _repo()
    svc = AreaService(repo, _geocoder(_city_result(cc="it")))
    assert await svc.resolve_city("Hoi An", "vn") is None


async def test_non_city_feature_refused() -> None:
    repo = _repo()
    svc = AreaService(repo, _geocoder(_city_result(place_type="route")))
    assert await svc.resolve_city("Hoi An", "vn") is None


async def test_province_admin_level_accepted() -> None:
    # Ha Giang is administrative_area_level_1 — provinces are city-scope
    # (ADR-124), so admin levels verify.
    result = _city_result(
        name="Ha Giang", place_type="administrative_area_level_1", city="Ha Giang"
    )
    svc = AreaService(_repo(), _geocoder(result))
    entity = await svc.resolve_city("Ha Giang", "vn")
    assert entity is not None
    assert entity.entity_key == "vn/ha-giang"


async def test_geocoder_error_refuses_not_raises() -> None:
    geocoder = _geocoder()
    geocoder.search_area = AsyncMock(side_effect=RuntimeError("down"))
    svc = AreaService(_repo(), geocoder)
    assert await svc.resolve_city("Hoi An", "vn") is None


async def test_memo_avoids_repeat_store_reads() -> None:
    repo = _repo(get_result=_stored())
    svc = AreaService(repo, _geocoder())
    await svc.resolve_city("Hoi An", "vn")
    await svc.resolve_city("Hoi An", "vn")
    repo.get.assert_awaited_once()


# ---- resolve_country -------------------------------------------------------


async def test_country_alpha2_store_hit() -> None:
    stored = _stored(key="vn", name="Vietnam", entity_type="country")
    repo = _repo(get_result=stored)
    geocoder = _geocoder()
    svc = AreaService(repo, geocoder)
    entity = await svc.resolve_country("vn")
    assert entity is not None and entity.entity_key == "vn"
    geocoder.search_area.assert_not_awaited()


async def test_country_by_name_verifies_feature_type() -> None:
    result = GeocodeResult(
        lat=14.0,
        lng=108.0,
        name="Vietnam",
        country="Vietnam",
        country_code="vn",
        place_type="country",
        provider_id="google:ChIJvn",
    )
    repo = _repo()
    svc = AreaService(repo, _geocoder(result))
    entity = await svc.resolve_country("Vietnam")
    assert entity is not None and entity.entity_key == "vn"
    repo.upsert.assert_awaited_once()


async def test_country_matching_non_country_feature_refused() -> None:
    result = _city_result(name="Japan Curry House", place_type="locality")
    svc = AreaService(_repo(), _geocoder(result))
    assert await svc.resolve_country("Japan Curry House") is None


# ---- resolve_noted_name (subject vs container) -----------------------------


async def test_noted_area_resolves_to_itself() -> None:
    svc = AreaService(_repo(), _geocoder(_city_result()))
    entity = await svc.resolve_noted_name(
        "Hoi An", AreaContext(country_code="vn")
    )
    assert entity is not None and entity.entity_key == "vn/hoi-an"


async def test_noted_route_collapses_to_containing_city() -> None:
    # "Ha Giang Loop" refuses as an area; the context city resolves.
    geocoder = _geocoder()

    async def _search(*, query: str, region_code: str | None = None):
        if query == "Ha Giang":
            return _city_result(name="Ha Giang", city="Ha Giang")
        return None

    geocoder.search_area = AsyncMock(side_effect=_search)
    svc = AreaService(_repo(), geocoder)
    entity = await svc.resolve_noted_name(
        "Ha Giang Loop", AreaContext(city="Ha Giang", country_code="vn")
    )
    assert entity is not None and entity.entity_key == "vn/ha-giang"


async def test_noted_route_skips_name_probe_when_flagged() -> None:
    geocoder = _geocoder(_city_result(name="Ha Giang", city="Ha Giang"))
    svc = AreaService(_repo(), geocoder)
    entity = await svc.resolve_noted_name(
        "Ha Giang Loop",
        AreaContext(city="Ha Giang", country_code="vn"),
        probe_name=False,
    )
    assert entity is not None and entity.entity_key == "vn/ha-giang"
    # Only the containing-city lookup ran — never the route name.
    queries = [c.kwargs["query"] for c in geocoder.search_area.await_args_list]
    assert queries == ["Ha Giang"]


async def test_noted_without_city_falls_back_to_country() -> None:
    svc = AreaService(_repo(), _geocoder(None))
    stored_country = _stored(key="vn", name="Vietnam", entity_type="country")
    svc._repo.get = AsyncMock(  # type: ignore[attr-defined]
        side_effect=lambda k: stored_country if k == "vn" else None
    )
    entity = await svc.resolve_noted_name(
        "Ha Long Bay", AreaContext(country_code="vn")
    )
    assert entity is not None and entity.entity_key == "vn"


async def test_noted_without_any_context_refuses() -> None:
    svc = AreaService(_repo(), _geocoder(None))
    assert await svc.resolve_noted_name("Ha Giang Loop", AreaContext()) is None


# ---- geometry compliance refresh ------------------------------------------


async def test_stale_geometry_refreshes_through_place_id() -> None:
    stale = _stored(refreshed_days_ago=45)
    repo = _repo(get_result=stale)
    geocoder = _geocoder()
    geocoder.geocode_place_id = AsyncMock(
        return_value=GeocodeResult(lat=15.9, lng=108.4, bbox=[1, 2, 3, 4])
    )
    svc = AreaService(repo, geocoder)
    entity = await svc.get("vn/hoi-an")
    assert entity is not None
    assert entity.lat == 15.9
    assert entity.bbox == [1, 2, 3, 4]
    geocoder.geocode_place_id.assert_awaited_once_with("google:ChIJx")
    repo.update_geometry.assert_awaited_once()


async def test_fresh_geometry_skips_refresh() -> None:
    repo = _repo(get_result=_stored(refreshed_days_ago=1))
    geocoder = _geocoder()
    svc = AreaService(repo, geocoder)
    await svc.get("vn/hoi-an")
    geocoder.geocode_place_id.assert_not_awaited()


async def test_failed_refresh_serves_stored_geometry() -> None:
    stale = _stored(refreshed_days_ago=45)
    repo = _repo(get_result=stale)
    geocoder = _geocoder()
    geocoder.geocode_place_id = AsyncMock(side_effect=RuntimeError("down"))
    svc = AreaService(repo, geocoder)
    entity = await svc.get("vn/hoi-an")
    assert entity is not None and entity.lat == stale.lat
