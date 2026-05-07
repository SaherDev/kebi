"""Unit tests for ConsultService (feature 028 M4 shape)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.api.schemas.consult import Location
from kebi.core.consult.service import ConsultService
from kebi.core.consult.types import NoMatchesError
from kebi.core.places.filters import ConsultFilters
from kebi.core.places.models import (
    LocationContext,
    PlaceAttributes,
    PlaceObject,
    PlaceType,
)
from kebi.core.taste.schemas import Chip, ChipStatus, TasteProfile


def _place(place_id: str, provider_id: str | None = None) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        provider_id=provider_id or f"google:{place_id}",
        place_name=place_id,
        place_type=PlaceType.food_and_drink,
        subcategory="restaurant",
        tags=[],
        attributes=PlaceAttributes(location_context=LocationContext(city="Bangkok")),
        source=None,
        source_url=None,
        lat=13.75,
        lng=100.50,
        enriched=True,
    )


def _build_service(
    *,
    chips: list[Chip] | None = None,
    discovered_places: list[PlaceObject] | None = None,
) -> tuple[ConsultService, dict[str, Any]]:
    discovered_places = discovered_places or []

    places_client = AsyncMock()
    places_client.discover = AsyncMock(
        return_value=[
            {
                "place_id": p.place_id,
                "name": p.place_name,
                "types": ["restaurant"],
                "vicinity": "somewhere",
                "geometry": {"location": {"lat": p.lat, "lng": p.lng}},
                "_subcategory": p.subcategory,
            }
            for p in discovered_places
        ]
    )
    places_client.geocode = AsyncMock(return_value=None)

    places_service = AsyncMock()
    places_service.enrich_batch = AsyncMock(
        side_effect=lambda places, **_: list(places)
    )

    taste_service = AsyncMock()
    profile = TasteProfile(
        taste_profile_summary=[],
        signal_counts={"totals": {"saves": 3}},
        chips=chips or [],
        generated_from_log_count=3,
    )
    taste_service.get_taste_profile = AsyncMock(return_value=profile)

    recommendation_repo = MagicMock()
    recommendation_repo.save = AsyncMock(return_value="rec_id_stub")

    service = ConsultService(
        places_client=places_client,
        places_service=places_service,
        taste_service=taste_service,
        recommendation_repo=recommendation_repo,
    )
    return service, {
        "places_client": places_client,
        "taste_service": taste_service,
    }


def _map_google_to_place_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub map_google_place_to_place_object to return a simple PlaceObject."""
    from kebi.core.consult import service as svc_module

    def _stub(raw: dict[str, Any]) -> PlaceObject:
        return PlaceObject(
            place_id=raw["place_id"],
            provider_id=f"google:{raw['place_id']}",
            place_name=raw["name"],
            place_type=PlaceType.food_and_drink,
            subcategory=raw.get("_subcategory", "restaurant"),
            tags=[],
            attributes=PlaceAttributes(
                location_context=LocationContext(city="Bangkok")
            ),
            source=None,
            source_url=None,
            lat=raw["geometry"]["location"]["lat"],
            lng=raw["geometry"]["location"]["lng"],
            enriched=True,
        )

    monkeypatch.setattr(svc_module, "map_google_place_to_place_object", _stub)


# ---------------------------------------------------------------------------
# Emit-callback contract (FR-035(h))
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_callback_fires_pipeline_steps_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    service, _ = _build_service(discovered_places=[_place("disc_1")])
    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.consult(
        user_id="u1",
        query="Thai food",
        saved_places=[_place("saved_1")],
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
        emit=spy,
    )

    steps = [s for s, _ in emitted]
    # No geocode (no search_location_name). Discovery is split into
    # parallel keyword + suggestion emits, then merge → dedupe → enrich.
    assert "consult.keywords" in steps
    assert "consult.suggestions" in steps
    assert steps.index("consult.merge") < steps.index("consult.dedupe")
    assert steps.index("consult.dedupe") < steps.index("consult.enrich")
    assert "consult.geocode" not in steps


@pytest.mark.asyncio
async def test_emit_geocode_fires_when_search_location_name_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    service, deps = _build_service(discovered_places=[_place("disc_1")])
    deps["places_client"].geocode = AsyncMock(return_value={"lat": 1.0, "lng": 2.0})

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.consult(
        user_id="u1",
        query="Thai food",
        saved_places=[],
        filters=ConsultFilters(search_location_name="Shibuya"),
        limit=3,
        location=None,
        emit=spy,
    )

    steps = [s for s, _ in emitted]
    assert steps[0] == "consult.geocode"


@pytest.mark.asyncio
async def test_no_matches_raises_when_everything_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    service, _ = _build_service(discovered_places=[])

    with pytest.raises(NoMatchesError):
        await service.consult(
            user_id="u1",
            query="Thai food",
            saved_places=[],
            filters=ConsultFilters(),
            limit=3,
            location=Location(lat=13.75, lng=100.5),
        )


# ---------------------------------------------------------------------------
# Tier-specific branches (ADR-061)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warming_tier_applies_candidate_blend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    saved_places = [_place(f"saved_{i}") for i in range(3)]
    discovered_places = [_place(f"disc_{i}") for i in range(5)]

    service, _ = _build_service(discovered_places=discovered_places)

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    response = await service.consult(
        user_id="warm_user",
        query="Thai food",
        saved_places=saved_places,
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
        signal_tier="warming",
        emit=spy,
    )

    saved_in_results = [r for r in response.results if r.source == "saved"]
    discovered_in_results = [r for r in response.results if r.source == "discovered"]
    assert len(saved_in_results) == 1
    assert len(discovered_in_results) == 2

    tier_blend_summaries = [s for step, s in emitted if step == "consult.tier_blend"]
    assert len(tier_blend_summaries) == 1
    assert "1 from your saves" in tier_blend_summaries[0]
    assert "2 new discoveries" in tier_blend_summaries[0]


@pytest.mark.asyncio
async def test_active_tier_excludes_rejected_chip_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    restaurant = PlaceObject(
        place_id="rest_1",
        provider_id="google:rest_1",
        place_name="A restaurant",
        place_type=PlaceType.food_and_drink,
        subcategory="restaurant",
        tags=[],
        attributes=PlaceAttributes(),
        source=None,
        source_url=None,
        lat=13.75,
        lng=100.5,
        enriched=True,
    )
    cafe = PlaceObject(
        place_id="cafe_1",
        provider_id="google:cafe_1",
        place_name="A cafe",
        place_type=PlaceType.food_and_drink,
        subcategory="cafe",
        tags=[],
        attributes=PlaceAttributes(),
        source=None,
        source_url=None,
        lat=13.75,
        lng=100.5,
        enriched=True,
    )

    rejected_chip = Chip(
        label="No restaurants",
        source_field="subcategory.food_and_drink",
        source_value="restaurant",
        signal_count=3,
        status=ChipStatus.REJECTED,
        selection_round="round_1",
    )

    service, _ = _build_service(
        chips=[rejected_chip],
        discovered_places=[restaurant, cafe],
    )

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    response = await service.consult(
        user_id="active_user",
        query="food",
        saved_places=[],
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
        signal_tier="active",
        emit=spy,
    )

    place_ids = [r.place.place_id for r in response.results]
    assert "rest_1" not in place_ids
    assert "cafe_1" in place_ids

    chip_filter_summaries = [s for step, s in emitted if step == "consult.chip_filter"]
    # One filter emit (rejected) — no confirmed chips here.
    assert any("Removed 1 place" in s for s in chip_filter_summaries)


@pytest.mark.asyncio
async def test_active_tier_surfaces_confirmed_chips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    confirmed_chip = Chip(
        label="Ramen lover",
        source_field="attributes.cuisine",
        source_value="ramen",
        signal_count=5,
        status=ChipStatus.CONFIRMED,
        selection_round="round_1",
    )

    service, _ = _build_service(
        chips=[confirmed_chip],
        discovered_places=[_place("disc_1")],
    )

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.consult(
        user_id="active_user",
        query="Ramen nearby",
        saved_places=[_place("saved_1")],
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
        signal_tier="active",
        emit=spy,
    )

    chip_filter_summaries = [s for step, s in emitted if step == "consult.chip_filter"]
    assert any(
        "Honoring your confirmed preferences: Ramen lover" in s
        for s in chip_filter_summaries
    )


@pytest.mark.asyncio
async def test_non_warming_tier_skips_blend_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    service, _ = _build_service(
        discovered_places=[_place("disc_1"), _place("disc_2")],
    )

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.consult(
        user_id="active_user",
        query="Ramen nearby",
        saved_places=[_place("saved_1"), _place("saved_2")],
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
        signal_tier="active",
        emit=spy,
    )

    assert not any(step == "consult.tier_blend" for step, _ in emitted)


# ---------------------------------------------------------------------------
# ConsultResponse no longer carries reasoning_steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_has_no_reasoning_steps_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _map_google_to_place_object(monkeypatch)

    service, _ = _build_service(discovered_places=[_place("disc_1")])

    response = await service.consult(
        user_id="u1",
        query="Thai food",
        saved_places=[],
        filters=ConsultFilters(),
        limit=3,
        location=Location(lat=13.75, lng=100.5),
    )

    assert not hasattr(response, "reasoning_steps")
