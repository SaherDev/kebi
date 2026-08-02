"""Tests for ResearchEntityResolver — staged, verified-or-refuse resolution.

The invariant under test everywhere: the returned key is always derived
from the *asked-about* entity or the resolver clarifies — never a nearby
or stale entity's key (the Da Nang ≠ Koh Samui case).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.agent.location import WorkingLocation
from kebi.core.areas.models import AreaEntity
from kebi.core.knowledge.research_resolver import ResearchEntityResolver
from kebi.core.knowledge.schemas import ResolvedGeo, _slugify


def _wl(
    *,
    city: str = "Koh Samui",
    country: str = "Thailand",
    country_code: str | None = "th",
    neighborhood: str | None = None,
) -> WorkingLocation:
    return WorkingLocation(
        country=country,
        city=city,
        neighborhood=neighborhood,
        country_code=country_code,
        lat=9.5,
        lng=100.0,
    )


def _entity(geo: ResolvedGeo | None) -> AreaEntity | None:
    """Wrap a test's ResolvedGeo shorthand into the AreaEntity the
    AreaService now returns."""
    if geo is None:
        return None
    if geo.city:
        return AreaEntity(
            entity_key=f"{geo.country_code}/{_slugify(geo.city)}",
            entity_type="city",
            name=geo.city,
            country_code=geo.country_code or "",
            lat=0.0,
            lng=0.0,
        )
    return AreaEntity(
        entity_key=geo.country_code or "",
        entity_type="country",
        name=(geo.country_code or "").upper(),
        country_code=geo.country_code or "",
        lat=0.0,
        lng=0.0,
    )


def _geo(
    *,
    cities: dict[tuple[str, str], ResolvedGeo | None] | None = None,
    countries: dict[str, ResolvedGeo | None] | None = None,
) -> AsyncMock:
    """Stub AreaService keyed by (name, country_code) / name."""
    geo = AsyncMock()

    async def _city(name: str, country_code: str) -> AreaEntity | None:
        return _entity((cities or {}).get((name, country_code)))

    async def _country(name: str) -> AreaEntity | None:
        return _entity((countries or {}).get(name))

    geo.resolve_city = AsyncMock(side_effect=_city)
    geo.resolve_country = AsyncMock(side_effect=_country)
    return geo


# ---------------------------------------------------------------------------
# Stage 1 — exact working-location match
# ---------------------------------------------------------------------------


async def test_exact_match_uses_working_location_without_geocode() -> None:
    geo = _geo()
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(
        city="Koh Samui", working_location=_wl(country_code="th")
    )

    assert entity.entity_key == "th/koh-samui"
    assert entity.entity_type == "city"
    assert entity.confidence == 1.0
    assert not entity.needs_clarification
    geo.resolve_city.assert_not_awaited()
    geo.resolve_country.assert_not_awaited()


async def test_exact_match_tolerates_diacritic_spelling() -> None:
    resolver = ResearchEntityResolver(_geo())
    wl = _wl(city="Đà Nẵng", country="Vietnam", country_code="vn")

    entity = await resolver.resolve(city="Da Nang", working_location=wl)

    assert entity.entity_key == "vn/da-nang"
    assert entity.confidence == 1.0


async def test_working_location_without_code_falls_back_to_resolve_country() -> None:
    """A state checkpointed before `country_code` existed still resolves."""
    geo = _geo(countries={"Thailand": ResolvedGeo(country_code="th")})
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(
        city="Koh Samui", working_location=_wl(country_code=None)
    )

    assert entity.entity_key == "th/koh-samui"
    assert entity.confidence == 1.0
    geo.resolve_country.assert_awaited_once_with("Thailand")


async def test_named_neighborhood_rides_the_verified_city_key() -> None:
    resolver = ResearchEntityResolver(_geo())
    wl = _wl(city="Da Nang", country="Vietnam", country_code="vn")

    entity = await resolver.resolve(
        city="Da Nang", neighborhood="My Khe", working_location=wl
    )

    assert entity.entity_key == "vn/da-nang/my-khe"
    assert entity.entity_type == "neighborhood"
    assert entity.entity_name == "My Khe"


async def test_no_named_area_researches_the_working_location() -> None:
    resolver = ResearchEntityResolver(_geo())

    entity = await resolver.resolve(working_location=_wl())

    assert entity.entity_key == "th/koh-samui"
    assert entity.entity_type == "city"
    assert entity.confidence == 1.0


# ---------------------------------------------------------------------------
# Stage 2 — verified geocode; the Da Nang ≠ Koh Samui invariant
# ---------------------------------------------------------------------------


async def test_mismatching_city_never_returns_working_location_key() -> None:
    """The headline bug: asked about Da Nang from a stale Koh Samui turn."""
    geo = _geo(
        countries={"Vietnam": ResolvedGeo(country_code="vn")},
        cities={("Da Nang", "vn"): ResolvedGeo(country_code="vn", city="Da Nang")},
    )
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(
        city="Da Nang", country="Vietnam", working_location=_wl()
    )

    assert entity.entity_key == "vn/da-nang"
    assert entity.entity_key != "th/koh-samui"
    assert entity.confidence == 0.8


async def test_agent_country_constrains_before_working_location_country() -> None:
    """Cross-country: the agent-passed country is tried first."""
    geo = _geo(
        countries={"Vietnam": ResolvedGeo(country_code="vn")},
        cities={("Da Nang", "vn"): ResolvedGeo(country_code="vn", city="Da Nang")},
    )
    resolver = ResearchEntityResolver(geo)

    await resolver.resolve(city="Da Nang", country="Vietnam", working_location=_wl())

    first_call = geo.resolve_city.await_args_list[0]
    assert first_call.args == ("Da Nang", "vn")  # not the stale "th"


async def test_falls_back_to_working_location_country_when_no_agent_country() -> None:
    geo = _geo(
        cities={("Chiang Mai", "th"): ResolvedGeo(country_code="th", city="Chiang Mai")}
    )
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(city="Chiang Mai", working_location=_wl())

    assert entity.entity_key == "th/chiang-mai"
    assert entity.confidence == 0.8


async def test_country_arg_contradicting_working_location_skips_stage_one() -> None:
    """Same city name, different country: 'Paris, Texas' from a Paris/FR turn."""
    geo = _geo(
        countries={"United States": ResolvedGeo(country_code="us")},
        cities={("Paris", "us"): ResolvedGeo(country_code="us", city="Paris")},
    )
    resolver = ResearchEntityResolver(geo)
    wl = _wl(city="Paris", country="France", country_code="fr")

    entity = await resolver.resolve(
        city="Paris", country="United States", working_location=wl
    )

    assert entity.entity_key == "us/paris"


# ---------------------------------------------------------------------------
# Country scope
# ---------------------------------------------------------------------------


async def test_country_question_resolves_verified() -> None:
    geo = _geo(countries={"Vietnam": ResolvedGeo(country_code="vn")})
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(country="Vietnam", working_location=_wl())

    assert entity.entity_key == "vn"
    assert entity.entity_type == "country"
    assert entity.confidence == 0.8


async def test_country_matching_working_location_skips_geocode() -> None:
    geo = _geo()
    resolver = ResearchEntityResolver(geo)

    entity = await resolver.resolve(country="Thailand", working_location=_wl())

    assert entity.entity_key == "th"
    assert entity.confidence == 1.0
    geo.resolve_country.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stage 3 — clarify, never a wrong key
# ---------------------------------------------------------------------------


async def test_unverifiable_city_clarifies_unresolved() -> None:
    resolver = ResearchEntityResolver(_geo())  # every lookup misses

    entity = await resolver.resolve(city="Atlantis", working_location=_wl())

    assert entity.needs_clarification
    assert entity.entity_key is None
    assert entity.empty_reason == "unresolved"
    assert entity.clarification_reason


async def test_city_with_no_country_context_clarifies_ambiguous() -> None:
    resolver = ResearchEntityResolver(_geo())

    entity = await resolver.resolve(city="Springfield", working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_no_area_and_no_working_location_clarifies_ambiguous() -> None:
    resolver = ResearchEntityResolver(_geo())

    entity = await resolver.resolve(working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_neighborhood_alone_without_city_context_clarifies() -> None:
    resolver = ResearchEntityResolver(_geo())

    entity = await resolver.resolve(neighborhood="My Khe", working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_confidence_below_min_clarifies() -> None:
    geo = _geo(
        cities={("Chiang Mai", "th"): ResolvedGeo(country_code="th", city="Chiang Mai")}
    )
    resolver = ResearchEntityResolver(geo, confidence_min=0.9)

    entity = await resolver.resolve(city="Chiang Mai", working_location=_wl())

    assert entity.needs_clarification  # geocoded 0.8 < 0.9 min
    assert entity.empty_reason == "unresolved"
