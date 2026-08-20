"""Tests for ResearchEntityResolver — staged, verified-or-refuse resolution.

The invariant under test everywhere: the returned key is always derived
from the *asked-about* entity or the resolver clarifies — never a nearby
or stale entity's key (the Da Nang ≠ Koh Samui case).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.agent.location import WorkingLocation
from kebi.core.knowledge.research_resolver import ResearchEntityResolver
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city, make_country

_THAILAND = make_country("th", "Thailand")
_KOH_SAMUI = make_city("th", "Koh Samui")
_CHIANG_MAI = make_city("th", "Chiang Mai")
_VIETNAM = make_country("vn", "Vietnam")
_DA_NANG = make_city("vn", "Da Nang")
_MY_KHE = make_area(_DA_NANG, "My Khe")
_US = make_country("us", "United States")
_PARIS_US = make_city("us", "Paris")


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


def _registry(*rows) -> FakeGeoRegistry:
    """Seeded registry with spies on the two resolution entry points."""
    registry = FakeGeoRegistry(*rows)
    registry.resolve_country = AsyncMock(  # type: ignore[method-assign]
        wraps=registry.resolve_country
    )
    registry.key_for_location = AsyncMock(  # type: ignore[method-assign]
        wraps=registry.key_for_location
    )
    return registry


# ---------------------------------------------------------------------------
# Stage 1 — exact working-location match
# ---------------------------------------------------------------------------


async def test_exact_match_keys_off_the_working_locations_country() -> None:
    registry = _registry(_KOH_SAMUI)
    resolver = ResearchEntityResolver(registry)

    entity = await resolver.resolve(
        city="Koh Samui", working_location=_wl(country_code="th")
    )

    assert entity.entity_key == _KOH_SAMUI.geo_key
    assert entity.entity_type == "city"
    assert entity.confidence == 1.0
    assert not entity.needs_clarification
    # The verified code came off the working location — no name resolve.
    registry.resolve_country.assert_not_awaited()  # type: ignore[union-attr]


async def test_exact_match_tolerates_diacritic_spelling() -> None:
    resolver = ResearchEntityResolver(_registry(_DA_NANG))
    wl = _wl(city="Đà Nẵng", country="Vietnam", country_code="vn")

    entity = await resolver.resolve(city="Da Nang", working_location=wl)

    assert entity.entity_key == _DA_NANG.geo_key
    assert entity.confidence == 1.0


async def test_working_location_without_code_falls_back_to_resolve_country() -> None:
    """A state checkpointed before `country_code` existed still resolves."""
    registry = _registry(_THAILAND, _KOH_SAMUI)
    resolver = ResearchEntityResolver(registry)

    entity = await resolver.resolve(
        city="Koh Samui", working_location=_wl(country_code=None)
    )

    assert entity.entity_key == _KOH_SAMUI.geo_key
    assert entity.confidence == 1.0
    registry.resolve_country.assert_awaited_once_with("Thailand")  # type: ignore[union-attr]


async def test_verified_neighborhood_resolves_as_a_neighborhood_entity() -> None:
    resolver = ResearchEntityResolver(_registry(_DA_NANG, _MY_KHE))
    wl = _wl(city="Da Nang", country="Vietnam", country_code="vn")

    entity = await resolver.resolve(
        city="Da Nang", neighborhood="My Khe", working_location=wl
    )

    assert entity.entity_key == _MY_KHE.geo_key
    assert entity.entity_type == "neighborhood"
    assert entity.entity_name == "My Khe"


async def test_unverified_neighborhood_degrades_to_the_city_entity() -> None:
    """A neighborhood the registry can't verify no longer rides the city key
    under its own name — the read degrades to the verified CITY entity, so
    the ancestors still answer and no key is minted for an unverified name."""
    resolver = ResearchEntityResolver(_registry(_DA_NANG))
    wl = _wl(city="Da Nang", country="Vietnam", country_code="vn")

    entity = await resolver.resolve(
        city="Da Nang", neighborhood="Nowhere Beach", working_location=wl
    )

    assert entity.entity_key == _DA_NANG.geo_key
    assert entity.entity_type == "city"
    assert entity.entity_name == "Da Nang"


async def test_no_named_area_researches_the_working_location() -> None:
    resolver = ResearchEntityResolver(_registry(_KOH_SAMUI))

    entity = await resolver.resolve(working_location=_wl())

    assert entity.entity_key == _KOH_SAMUI.geo_key
    assert entity.entity_type == "city"
    assert entity.confidence == 1.0


# ---------------------------------------------------------------------------
# Stage 2 — verified registry resolve; the Da Nang ≠ Koh Samui invariant
# ---------------------------------------------------------------------------


async def test_mismatching_city_never_returns_working_location_key() -> None:
    """The headline bug: asked about Da Nang from a stale Koh Samui turn."""
    resolver = ResearchEntityResolver(_registry(_VIETNAM, _DA_NANG))

    entity = await resolver.resolve(
        city="Da Nang", country="Vietnam", working_location=_wl()
    )

    assert entity.entity_key == _DA_NANG.geo_key
    assert entity.entity_key != _KOH_SAMUI.geo_key
    assert entity.confidence == 0.8


async def test_agent_country_constrains_before_working_location_country() -> None:
    """Cross-country: the agent-passed country is tried first."""
    registry = _registry(_VIETNAM, _DA_NANG)
    resolver = ResearchEntityResolver(registry)

    await resolver.resolve(city="Da Nang", country="Vietnam", working_location=_wl())

    first_call = registry.key_for_location.await_args_list[0]  # type: ignore[union-attr]
    assert first_call.args == ("vn", "Da Nang", None)  # not the stale "th"


async def test_falls_back_to_working_location_country_when_no_agent_country() -> None:
    resolver = ResearchEntityResolver(_registry(_CHIANG_MAI))

    entity = await resolver.resolve(city="Chiang Mai", working_location=_wl())

    assert entity.entity_key == _CHIANG_MAI.geo_key
    assert entity.confidence == 0.8


async def test_country_arg_contradicting_working_location_skips_stage_one() -> None:
    """Same city name, different country: 'Paris, Texas' from a Paris/FR turn."""
    resolver = ResearchEntityResolver(_registry(_US, _PARIS_US))
    wl = _wl(city="Paris", country="France", country_code="fr")

    entity = await resolver.resolve(
        city="Paris", country="United States", working_location=wl
    )

    assert entity.entity_key == _PARIS_US.geo_key


# ---------------------------------------------------------------------------
# Country scope
# ---------------------------------------------------------------------------


async def test_country_question_resolves_verified() -> None:
    resolver = ResearchEntityResolver(_registry(_VIETNAM))

    entity = await resolver.resolve(country="Vietnam", working_location=_wl())

    assert entity.entity_key == "vn"
    assert entity.entity_type == "country"
    assert entity.confidence == 0.8


async def test_country_matching_working_location_skips_the_registry() -> None:
    registry = _registry()
    resolver = ResearchEntityResolver(registry)

    entity = await resolver.resolve(country="Thailand", working_location=_wl())

    assert entity.entity_key == "th"
    assert entity.confidence == 1.0
    registry.resolve_country.assert_not_awaited()  # type: ignore[union-attr]


async def test_literal_alpha2_code_bypasses_the_registry() -> None:
    registry = _registry()
    resolver = ResearchEntityResolver(registry)

    entity = await resolver.resolve(country="jp", working_location=None)

    assert entity.entity_key == "jp"
    assert entity.entity_type == "country"
    assert entity.confidence == 0.8
    registry.resolve_country.assert_not_awaited()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Stage 3 — clarify, never a wrong key
# ---------------------------------------------------------------------------


async def test_unverifiable_city_clarifies_unresolved() -> None:
    resolver = ResearchEntityResolver(_registry())  # every lookup misses

    entity = await resolver.resolve(city="Atlantis", working_location=_wl())

    assert entity.needs_clarification
    assert entity.entity_key is None
    assert entity.empty_reason == "unresolved"
    assert entity.clarification_reason


async def test_city_with_no_country_context_clarifies_ambiguous() -> None:
    resolver = ResearchEntityResolver(_registry())

    entity = await resolver.resolve(city="Springfield", working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_no_area_and_no_working_location_clarifies_ambiguous() -> None:
    resolver = ResearchEntityResolver(_registry())

    entity = await resolver.resolve(working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_neighborhood_alone_without_city_context_clarifies() -> None:
    resolver = ResearchEntityResolver(_registry())

    entity = await resolver.resolve(neighborhood="My Khe", working_location=None)

    assert entity.needs_clarification
    assert entity.empty_reason == "ambiguous"


async def test_confidence_below_min_clarifies() -> None:
    resolver = ResearchEntityResolver(_registry(_CHIANG_MAI), confidence_min=0.9)

    entity = await resolver.resolve(city="Chiang Mai", working_location=_wl())

    assert entity.needs_clarification  # resolved at 0.8 < 0.9 min
    assert entity.empty_reason == "unresolved"
