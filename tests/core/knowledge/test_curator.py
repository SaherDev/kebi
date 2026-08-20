"""Tests for KnowledgeCurator — prose structuring + geo resolution (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.curator import (
    KnowledgeCurator,
    _CuratedClaim,
    _CuratorResponse,
)
from kebi.core.knowledge.schemas import CurationAnchor, ResolvedGeo
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city, make_country

_DUBAI = make_city("ae", "Dubai")
_DEIRA = make_area(_DUBAI, "Deira")
_ABU_DHABI = make_city("ae", "Abu Dhabi")
_INDONESIA = make_country("id", "Indonesia")


def _curator(
    claims: list[_CuratedClaim], registry: FakeGeoRegistry | None = None
) -> tuple[KnowledgeCurator, FakeGeoRegistry]:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_CuratorResponse(claims=claims))
    registry = registry or FakeGeoRegistry()
    registry.key_for_location = AsyncMock(  # type: ignore[method-assign]
        wraps=registry.key_for_location
    )
    return KnowledgeCurator(client, registry), registry


_CLAIM = _CuratedClaim(
    scope="neighborhood",
    entity_name="Deira",
    area_query="Deira, Dubai, United Arab Emirates",
    claim="the gold souk haggling starts at half the sticker",
    tags=["nightlife"],
    confidence=0.8,
)

_PLACE_CLAIM = _CuratedClaim(
    scope="place",
    entity_name="Beach Club X",
    claim="cash only at the bar",
    tags=["money"],
    confidence=0.9,
)

_VENUE_ANCHOR = CurationAnchor(
    place_id="place-123",
    name="Beach Club X",
    geo=ResolvedGeo(country_code="ae", city="Dubai", neighborhood="Jumeirah"),
)

_AREA_ANCHOR = CurationAnchor(
    place_id=None,
    name="Jumeirah",
    geo=ResolvedGeo(country_code="ae", city="Dubai", neighborhood="Jumeirah"),
)


async def test_resolves_area_inside_the_anchor_city_via_registry() -> None:
    # A claim naming a different area than the anchor resolves through the
    # registry, constrained to the anchor's city — verified, never free-text.
    curator, _ = _curator([_CLAIM], FakeGeoRegistry(_DUBAI, _DEIRA))
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert len(out) == 1
    assert out[0].geo == ResolvedGeo(
        country_code="ae", city="Dubai", neighborhood="Deira"
    )


async def test_resolves_a_city_in_the_anchor_country() -> None:
    # Not an area of the anchor's city, but a city of its country — the
    # second rung of the constrained resolve.
    claim = _CLAIM.model_copy(
        update={"entity_name": "Abu Dhabi", "area_query": "Abu Dhabi", "scope": "city"}
    )
    curator, _ = _curator([claim], FakeGeoRegistry(_DUBAI, _ABU_DHABI))
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert out[0].geo == ResolvedGeo(country_code="ae", city="Abu Dhabi")


async def test_unanchored_country_name_still_resolves() -> None:
    # With no anchor there is no country to constrain a lookup, so only a
    # country name can verify — the free-text global geocode is gone.
    claim = _CuratedClaim(
        scope="country",
        entity_name="Indonesia",
        area_query="Indonesia",
        claim="cash still rules outside the cities",
        confidence=0.8,
    )
    curator, _ = _curator([claim], FakeGeoRegistry(_INDONESIA))
    out = await curator.structure("prose")
    assert len(out) == 1
    assert out[0].geo == ResolvedGeo(country_code="id")


async def test_falls_back_to_anchor_geo_when_resolve_fails() -> None:
    claim = _CLAIM.model_copy(
        update={"entity_name": "Al Quoz", "area_query": "Al Quoz"}
    )
    curator, _ = _curator([claim])  # registry knows nothing
    out = await curator.structure("prose", _AREA_ANCHOR)
    assert len(out) == 1
    assert out[0].geo == _AREA_ANCHOR.geo


async def test_dropped_when_no_resolve_and_no_anchor() -> None:
    # Even a registry that knows the area can't help: an unanchored query
    # has no country to constrain the lookup, and only country names
    # resolve unconstrained.
    curator, _ = _curator([_CLAIM], FakeGeoRegistry(_DUBAI, _DEIRA))
    assert await curator.structure("prose") == []


async def test_place_claim_keyed_to_venue_anchor() -> None:
    curator, registry = _curator([_PLACE_CLAIM])
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert len(out) == 1
    assert out[0].scope == "place"
    assert out[0].place_ref == "place-123"
    assert out[0].entity_name == "Beach Club X"
    # Nothing to resolve for the venue.
    registry.key_for_location.assert_not_awaited()  # type: ignore[union-attr]


async def test_place_claim_dropped_without_venue_anchor() -> None:
    curator, _ = _curator([_PLACE_CLAIM])
    assert await curator.structure("prose") == []


async def test_place_claim_dropped_under_area_anchor() -> None:
    curator, _ = _curator([_PLACE_CLAIM])
    assert await curator.structure("prose", _AREA_ANCHOR) == []


async def test_claim_naming_anchor_takes_anchor_geo_over_registry() -> None:
    # Live failure this pins (pre-registry as a geocode, same rule now): a
    # claim about the anchor itself must key to the anchor's own verified
    # geo, never re-resolve its name into a possibly different key that
    # would split the entity's claims.
    claim = _CuratedClaim(
        scope="neighborhood",
        entity_name="Canggu",
        area_query="Canggu, Bali, Indonesia",
        claim="rent a scooter; taxis are scarce",
        tags=["transport"],
        confidence=0.9,
    )
    anchor = CurationAnchor(
        place_id=None,
        name="Canggu",
        geo=ResolvedGeo(country_code="id", city="bali", neighborhood="canggu"),
    )
    curator, registry = _curator([claim])
    out = await curator.structure("prose", anchor)
    assert len(out) == 1
    assert out[0].geo.city == "bali"
    assert out[0].geo.neighborhood == "canggu"
    registry.key_for_location.assert_not_awaited()  # type: ignore[union-attr]


async def test_empty_area_query_under_anchor_means_here() -> None:
    claim = _CuratedClaim(
        scope="neighborhood",
        entity_name="Canggu",
        area_query="",
        claim="gojek pickups get blocked from gated streets",
        confidence=0.8,
    )
    curator, registry = _curator([claim])
    out = await curator.structure("prose", _AREA_ANCHOR)
    assert len(out) == 1
    assert out[0].geo == _AREA_ANCHOR.geo
    registry.key_for_location.assert_not_awaited()  # type: ignore[union-attr]


async def test_geo_spillover_under_venue_anchor() -> None:
    # An area-level remark in venue-anchored prose still lands geo-scoped,
    # falling back to the venue's own location when nothing verifies.
    spill = _CLAIM.model_copy(update={"entity_name": "Jumeirah", "area_query": ""})
    curator, _ = _curator([_PLACE_CLAIM, spill])
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert [c.scope for c in out] == ["place", "neighborhood"]
    assert out[1].geo.country_code == "ae"


async def test_anchor_line_rendered_into_system_prompt() -> None:
    curator, _ = _curator([])
    await curator.structure("prose", _VENUE_ANCHOR)
    system = curator._client.extract.await_args.kwargs["messages"][0]["content"]
    assert 'the venue "Beach Club X"' in system
    assert "ANCHORED REQUEST" in system


async def test_distinct_area_resolved_once() -> None:
    curator, registry = _curator([_CLAIM, _CLAIM], FakeGeoRegistry(_DUBAI, _DEIRA))
    await curator.structure("prose", _VENUE_ANCHOR)
    # Memoized per identical area_query.
    registry.key_for_location.assert_awaited_once()  # type: ignore[union-attr]


async def test_empty_text_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeCurator(client, FakeGeoRegistry()).structure("   ")
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeCurator(client, FakeGeoRegistry()).structure("prose")
    assert out == []
