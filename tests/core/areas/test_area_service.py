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

    # The canonical name's own slug is always an alias — that is what makes
    # "is this name already known to be an area?" answerable from the index
    # alone (the check that stops a pass being offered as a savable venue).
    # A diacritic variant folding to the same slug adds nothing beyond it.
    svc2 = AreaService(_repo(), _geocoder(_city_result(name="Hội An")))
    entity2 = await svc2.resolve_city("Hội An", "vn")
    assert entity2 is not None
    assert entity2.aliases == ["hoi-an"]


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
    entity = await svc.resolve_noted_name("Hoi An", AreaContext(country_code="vn"))
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
    entity = await svc.resolve_noted_name("Ha Long Bay", AreaContext(country_code="vn"))
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


# ---- resolve_area — the Step 6 widening ------------------------------------
#
# The locked roadmap rule these tests guard: bounded geography that the
# provider can verify is an area; a named journey is not. The separator is the
# round-trip check that already existed, NOT a list of route-sounding words.


def _area_result(
    *,
    name: str,
    place_type: str,
    city: str | None = None,
    neighborhood: str | None = None,
    cc: str = "vn",
) -> GeocodeResult:
    return GeocodeResult(
        lat=16.2,
        lng=108.13,
        name=name,
        city=city,
        neighborhood=neighborhood,
        country="Vietnam",
        country_code=cc,
        place_type=place_type,
        bbox=[16.19, 16.21, 108.12, 108.14],
        provider_id="google:ChIJpass",
    )


async def test_natural_feature_resolves_as_an_area_not_a_venue() -> None:
    """A pass is geography with extent — the model that dissolves the
    save-a-pass-as-a-restaurant hole."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="Hai Van Pass", place_type="natural_feature", city="Da Nang"
            )
        ),
    )
    entity = await svc.resolve_area("Hai Van Pass", "vn")
    assert entity is not None
    assert entity.entity_type == "natural_feature"
    assert entity.entity_key == "vn/da-nang/hai-van-pass"
    assert entity.parent_key == "vn/da-nang"


async def test_named_street_resolves_as_an_area() -> None:
    """A street the provider names the way people ask for it is an area — the
    narrowing of 'external routes are untrusted' this step records.

    Note the bar this sets in practice: verification still applies, and
    Google names the real Hanoi Train Street *Ngõ 224 Lê Duẩn*, which does not
    round-trip against what anyone would type. So streets resolve only where
    the provider agrees on the name. That is verified-or-refuse working, not a
    gap to paper over — the alternative is kebi asserting that an alley it
    cannot confirm is the famous one.
    """
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(name="Hanoi Train Street", place_type="route", city="Hanoi")
        ),
    )
    entity = await svc.resolve_area("Hanoi Train Street", "vn")
    assert entity is not None
    assert entity.entity_type == "street"
    assert entity.entity_key == "vn/hanoi/hanoi-train-street"


async def test_neighborhood_resolves_under_its_city() -> None:
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="An Thuong",
                place_type="sublocality_level_1",
                city="Da Nang",
                neighborhood="An Thuong",
            )
        ),
    )
    entity = await svc.resolve_area("An Thuong", "vn", city_hint="Da Nang")
    assert entity is not None
    assert entity.entity_type == "neighborhood"
    assert entity.entity_key == "vn/da-nang/an-thuong"


async def test_province_resolves_as_a_region_at_city_depth() -> None:
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="Quang Nam",
                place_type="administrative_area_level_1",
                city="Quang Nam",
            )
        ),
    )
    entity = await svc.resolve_area("Quang Nam", "vn")
    assert entity is not None
    assert entity.entity_type == "region"
    assert entity.entity_key == "vn/quang-nam"
    assert entity.parent_key == "vn"


async def test_named_journey_is_refused_and_persists_nothing() -> None:
    """THE locked decision: "Ha Giang Loop" has no footprint that round-trips,
    so it is refused and collapses to its containing area elsewhere. Widening
    the accepted types must never widen this."""
    repo = _repo()
    # The provider answers with the province the loop runs through — a real
    # feature, but not the thing that was asked for.
    svc = AreaService(
        repo,
        _geocoder(
            _area_result(
                name="Ha Giang",
                place_type="administrative_area_level_1",
                city="Ha Giang",
            )
        ),
    )
    assert await svc.resolve_area("Ha Giang Loop", "vn") is None
    repo.upsert.assert_not_awaited()


async def test_unmapped_feature_type_is_refused() -> None:
    """A type with no spec is not an area — acceptance is a closed set, so a
    new provider type can never silently become a kind."""
    svc = AreaService(
        _repo(),
        _geocoder(_area_result(name="Some Cafe", place_type="establishment")),
    )
    assert await svc.resolve_area("Some Cafe", "vn") is None


async def test_resolve_city_still_refuses_a_street() -> None:
    """`resolve_city` stays settlement-only — the corridor endpoint resolver
    depends on that narrowness (ADR-136)."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(name="Hanoi Train Street", place_type="route", city="Hanoi")
        ),
    )
    assert await svc.resolve_city("Hanoi Train Street", "vn") is None


# ---- round-trip: what real provider responses actually look like ------------
#
# Every case below is a response the live Google Geocoding API returned during
# Step 6 verification. They are here because the first cut of `resolve_area`
# refused most of them.


async def test_a_fuller_provider_name_still_round_trips() -> None:
    """Asked "My Khe", provider says "My Khe Beach" — the same feature under
    its fuller name. Strict equality refused exactly the areas this step
    exists to resolve."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="My Khe Beach", place_type="natural_feature", city="Da Nang"
            )
        ),
    )
    entity = await svc.resolve_area("My Khe", "vn", city_hint="Da Nang")
    assert entity is not None
    assert entity.name == "My Khe Beach"


async def test_containment_the_other_way_round_is_not_a_match() -> None:
    """ "Ha Giang Loop" contains "Ha Giang", not the reverse. The direction of
    containment is the whole difference between naming a place and naming a
    trip through it — and it is what keeps the locked rule locked."""
    repo = _repo()
    svc = AreaService(
        repo,
        _geocoder(
            _area_result(
                name="Ha Giang",
                place_type="administrative_area_level_1",
                city="Ha Giang",
            )
        ),
    )
    assert await svc.resolve_area("Ha Giang Loop", "vn") is None
    repo.upsert.assert_not_awaited()


async def test_a_partial_word_never_wraps() -> None:
    """ "An" must not match "An Thuong", or every short name resolves to
    something larger that happens to contain its letters."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="An Thuong", place_type="sublocality_level_1", city="Da Nang"
            )
        ),
    )
    assert await svc.resolve_area("Anh", "vn", city_hint="Da Nang") is None


async def test_a_country_result_never_satisfies_an_area_ask() -> None:
    """The provider answers almost any unresolvable query inside a country
    with that country. Accepting it here turned "Ha Giang Loop" into `vn`."""
    svc = AreaService(
        _repo(),
        _geocoder(
            GeocodeResult(
                lat=14.0,
                lng=108.0,
                name="Vietnam",
                country="Vietnam",
                country_code="vn",
                place_type="country",
                provider_id="google:ChIJvn",
            )
        ),
    )
    assert await svc.resolve_area("Ha Giang Loop", "vn") is None


async def test_a_neighborhood_outside_the_named_city_is_refused() -> None:
    """Asked for "An Thuong" near Da Nang, the bare-name fallback found a
    same-named neighborhood in Nho Quan, 500 km away."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(
                name="An Thuong",
                place_type="sublocality_level_1",
                city="Nho Quan",
                neighborhood="An Thuong",
            )
        ),
    )
    assert await svc.resolve_area("An Thuong", "vn", city_hint="Da Nang") is None


async def test_geography_may_sit_outside_the_named_city() -> None:
    """Hai Van Pass runs over the Da Nang / Hue boundary and the provider files
    it under Hue. Binding it to the asked city refused the very feature asked
    for — so only neighborhoods are bound to the hint."""
    svc = AreaService(
        _repo(),
        _geocoder(
            _area_result(name="Hai Van Pass", place_type="natural_feature", city="Hue")
        ),
    )
    entity = await svc.resolve_area("Hai Van Pass", "vn", city_hint="Da Nang")
    assert entity is not None
    assert entity.entity_key == "vn/hue/hai-van-pass"


async def test_a_stored_neighborhood_elsewhere_is_not_reused_by_alias() -> None:
    """Alias lookup is country-scoped, so once the wrong An Thuong was stored
    it matched by alias and skipped the geocode — the store turning one bad
    resolution into a permanent one."""
    stored = _stored(
        key="vn/nho-quan/an-thuong", name="An Thuong", entity_type="neighborhood"
    )
    stored = stored.model_copy(update={"parent_key": "vn/nho-quan"})
    geocoder = _geocoder(None)
    svc = AreaService(_repo(alias_result=stored), geocoder)
    assert await svc.resolve_area("An Thuong", "vn", city_hint="Da Nang") is None


async def test_the_bare_name_is_tried_when_the_hint_makes_it_worse() -> None:
    """ "Hai Van Pass, Da Nang" returns the road under its Vietnamese name
    (Đèo Hải Vân), which does not round-trip; the bare name returns the
    feature itself. So the fallback buys an answer where there was a refusal."""
    hinted = _area_result(name="Đèo Hải Vân", place_type="route", city="Da Nang")
    bare = _area_result(name="Hải Vân Pass", place_type="natural_feature", city="Hue")
    geocoder = _geocoder()
    geocoder.search_area = AsyncMock(side_effect=[hinted, bare])
    svc = AreaService(_repo(), geocoder)
    entity = await svc.resolve_area("Hai Van Pass", "vn", city_hint="Da Nang")
    assert entity is not None
    assert entity.entity_type == "natural_feature"
    assert geocoder.search_area.await_count == 2


async def test_one_geocode_when_the_hinted_query_already_verifies() -> None:
    geocoder = _geocoder(
        _area_result(
            name="Ngu Hanh Son",
            place_type="sublocality_level_1",
            city="Da Nang",
            neighborhood="Ngu Hanh Son",
        )
    )
    svc = AreaService(_repo(), geocoder)
    assert await svc.resolve_area("Ngu Hanh Son", "vn", city_hint="Da Nang")
    geocoder.search_area.assert_awaited_once()
