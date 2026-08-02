"""The Step 2 done-when scenario: a zero-venue share harvests through its
noted areas.

The Vietnam-video shape — Ha Giang Loop / Hoi An / Mui Ne, every one a
noted non-venue, zero venues saved — must produce area anchors (the route
collapsing to its containing area), harvest claims keyed to the resolved
areas with experience tags, and never a place-scoped claim.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.areas.models import AreaContext, AreaEntity
from kebi.core.knowledge.harvester import (
    KnowledgeHarvester,
    _HarvestedClaim,
    _HarvesterResponse,
)
from kebi.core.knowledge.schemas import HarvestContent, NotedAreaRef

_CONTENT = HarvestContent(
    caption="3 unreal stops in Vietnam: the Ha Giang Loop, Hoi An, Mui Ne",
    source_ref="https://tiktok.com/v/1",
)

_NOTED = [
    NotedAreaRef(
        name="Ha Giang Loop", country_code="vn", city=None, reason="non_venue_route"
    ),
    NotedAreaRef(name="Hoi An", country_code="vn", reason="non_venue_area"),
    NotedAreaRef(name="Mui Ne", country_code="vn", reason="non_venue_area"),
]

_ENTITIES = {
    "ha giang": AreaEntity(
        entity_key="vn/ha-giang",
        entity_type="city",
        name="Ha Giang",
        country_code="vn",
        lat=22.8,
        lng=105.0,
        bbox=[22.0, 23.3, 104.4, 105.6],
    ),
    "hoi an": AreaEntity(
        entity_key="vn/hoi-an",
        entity_type="city",
        name="Hoi An",
        country_code="vn",
        lat=15.88,
        lng=108.33,
        bbox=[15.8, 15.95, 108.2, 108.4],
    ),
    "mui ne": AreaEntity(
        entity_key="vn/mui-ne",
        entity_type="city",
        name="Mui Ne",
        country_code="vn",
        lat=10.95,
        lng=108.28,
        bbox=[10.9, 11.0, 108.2, 108.35],
    ),
}


def _area_service() -> AsyncMock:
    """Fake AreaService with the three Vietnamese areas resolvable. The
    route name itself never resolves; with no context city it falls to the
    country — this fake mirrors the real subject-vs-container behavior by
    honoring `resolve_noted_name` semantics directly."""
    svc = AsyncMock()

    async def _noted(
        name: str, context: AreaContext, *, probe_name: bool = True
    ) -> AreaEntity | None:
        if probe_name and name.lower() in _ENTITIES:
            return _ENTITIES[name.lower()]
        if context.city and context.city.lower() in _ENTITIES:
            return _ENTITIES[context.city.lower()]
        if name == "Ha Giang Loop":
            # Containing area known from the share's own text context.
            return _ENTITIES["ha giang"]
        return None

    async def _city(name: str, cc: str) -> AreaEntity | None:
        return _ENTITIES.get(name.lower())

    svc.resolve_noted_name = AsyncMock(side_effect=_noted)
    svc.resolve_city = AsyncMock(side_effect=_city)
    svc.resolve_country = AsyncMock(return_value=None)
    return svc


def _harvester(claims: list[_HarvestedClaim]) -> tuple[KnowledgeHarvester, AsyncMock]:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_HarvesterResponse(claims=claims))
    return KnowledgeHarvester(client, _area_service()), client


async def test_zero_venue_share_harvests_against_noted_areas() -> None:
    claims = [
        _HarvestedClaim(
            scope="city",
            place_index=0,  # the Ha Giang Loop anchor
            entity_name="Ha Giang Loop",
            claim="The Ha Giang Loop is a 350 km motorbike circuit through "
            "northern karst mountains.",
            tags=["motorbike_route", "scenic_route"],
            confidence=0.8,
        ),
        _HarvestedClaim(
            scope="city",
            place_index=1,
            entity_name="Hoi An",
            claim="Hoi An's old town glows with lanterns after sunset.",
            tags=["sunset_spot"],
            confidence=0.8,
        ),
        _HarvestedClaim(
            scope="city",
            place_index=2,
            entity_name="Mui Ne",
            claim="Mui Ne is known for its red and white sand dunes.",
            tags=[],
            confidence=0.7,
        ),
    ]
    harvester, client = _harvester(claims)
    out = await harvester.harvest(_CONTENT, [], _NOTED)

    # The LLM ran — zero venues no longer short-circuits the harvest.
    client.extract.assert_awaited_once()
    assert len(out) == 3
    # The route's claim keys to its CONTAINING area, not a route entity.
    assert out[0].geo is not None
    assert out[0].geo.city == "Ha Giang"
    assert out[0].tags == ["motorbike_route", "scenic_route"]
    assert out[1].geo.city == "Hoi An"
    assert out[2].geo.city == "Mui Ne"
    # Nothing venue-shaped came out of a venue-free share.
    assert all(c.place_ref is None for c in out)


async def test_anchor_list_renders_areas_with_marker() -> None:
    harvester, client = _harvester([])
    await harvester.harvest(_CONTENT, [], _NOTED)
    user_message = client.extract.await_args.kwargs["messages"][1]["content"]
    assert "[0] Ha Giang Loop (area of interest, not a venue)" in user_message
    assert "[1] Hoi An (area of interest, not a venue)" in user_message


async def test_place_scope_claim_on_area_anchor_dropped() -> None:
    claims = [
        _HarvestedClaim(
            scope="place",
            place_index=0,
            entity_name="Ha Giang Loop",
            claim="x",
            confidence=0.9,
        )
    ]
    harvester, _ = _harvester(claims)
    assert await harvester.harvest(_CONTENT, [], _NOTED) == []


async def test_unresolvable_noted_names_skip_but_do_not_block() -> None:
    noted = [
        NotedAreaRef(name="Atlantis", reason="non_venue_area"),  # no context
        *_NOTED[1:],
    ]
    harvester, client = _harvester([])
    await harvester.harvest(_CONTENT, [], noted)
    user_message = client.extract.await_args.kwargs["messages"][1]["content"]
    assert "Atlantis" not in user_message
    assert "Hoi An" in user_message


async def test_nothing_resolvable_and_no_venues_short_circuits() -> None:
    harvester, client = _harvester([])
    out = await harvester.harvest(
        _CONTENT, [], [NotedAreaRef(name="Atlantis", reason="non_venue_area")]
    )
    assert out == []
    client.extract.assert_not_awaited()


async def test_resolve_area_interests_returns_deduped_entities() -> None:
    """The Step-3 region-interest source: noted names resolve to their area
    entities (the route to its containing area), deduped by entity_key, with
    no LLM call — independent of the harvest itself."""
    harvester, client = _harvester([])
    entities = await harvester.resolve_area_interests(_NOTED)

    keys = [e.entity_key for e in entities]
    # Ha Giang Loop collapses to its containing area vn/ha-giang.
    assert set(keys) == {"vn/hoi-an", "vn/mui-ne", "vn/ha-giang"}
    client.extract.assert_not_awaited()


async def test_resolve_area_interests_skips_unresolvable() -> None:
    harvester, _ = _harvester([])
    entities = await harvester.resolve_area_interests(
        [NotedAreaRef(name="Atlantis", reason="non_venue_area"), *_NOTED[1:]]
    )
    keys = {e.entity_key for e in entities}
    assert "vn/hoi-an" in keys and "vn/mui-ne" in keys
    assert all("atlantis" not in k for k in keys)
