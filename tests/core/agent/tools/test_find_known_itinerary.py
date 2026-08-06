"""`find_known` on a multi-stop trip — claims fan out per segment (ADR-148)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langgraph.types import Command

from kebi.core.agent.location import ItineraryAnchor, WorkingLocation
from kebi.core.agent.tools.find_known_tool import _run_find_known
from kebi.core.knowledge.research_models import ResearchNote
from tests.core.agent.tools.test_find_known import _known, _payload, _state

_ANCHORS = [
    ItineraryAnchor(
        name="Hue",
        lat=16.46,
        lng=107.59,
        city="Hue",
        country="Vietnam",
        country_code="vn",
    ),
    ItineraryAnchor(
        name="Hoi An",
        lat=15.88,
        lng=108.33,
        city="Hoi An",
        country="Vietnam",
        country_code="vn",
    ),
]
# Two stops + one leg.
_SEGMENT_COUNT = 3


def _itinerary_working_location() -> dict[str, Any]:
    return WorkingLocation(
        country="Vietnam",
        country_code="vn",
        city="Hue",
        lat=16.46,
        lng=107.59,
        search_radius_m=7000.0,
        scope_shape="itinerary",
        scope_tier="city",
        effective_mode="driving",
        itinerary=_ANCHORS,
    ).model_dump()


async def _run(
    service: MagicMock, notes_service: Any = None, **kw: Any
) -> Command[Any]:
    return await _run_find_known(
        known_places=service,
        notes_service=notes_service,
        state=_state(working_location=_itinerary_working_location()),
        tool_call_id="tc-1",
        query=kw.pop("query", "what to stop for"),
        tags=None,
        limit=kw.pop("limit", 5),
    )


def _service(per_segment: list[list[Any]]) -> MagicMock:
    service = MagicMock()
    service.find = AsyncMock(side_effect=per_segment)
    return service


async def test_claims_are_read_for_every_stop_and_leg() -> None:
    service = _service([[] for _ in range(_SEGMENT_COUNT)])
    await _run(service)
    assert service.find.await_count == _SEGMENT_COUNT
    workings = [c.kwargs["working"] for c in service.find.await_args_list]
    assert [w.lat for w in workings] == [16.46, 15.88, (16.46 + 15.88) / 2]
    # A stop reads claims as ITS city — this is what lets Hue's schedule
    # facts surface for the Hue part of a trip anchored in another city.
    assert workings[1].city == "Hoi An"


async def test_known_places_carry_their_segment_and_dedupe_in_stop_order() -> None:
    quan_hanh = _known("Quan Hanh", "banh loc, cash only")
    service = _service([[quan_hanh], [], [quan_hanh]])
    cmd = await _run(service)
    candidates = _payload(cmd)["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["segment"] == "Hue"


async def test_area_notes_pool_per_stop_not_per_leg() -> None:
    def _note(note_id: str, text: str) -> ResearchNote:
        return ResearchNote(id=note_id, text=text, source="community", confidence=0.9)

    shared = _note("n-shared", "cash still rules outside the cities")
    notes_service = MagicMock()
    notes_service.notes_for_places = AsyncMock(return_value={})
    notes_service.notes_for_area = AsyncMock(
        side_effect=[
            [shared, _note("n-hue", "hue is a morning city")],
            [shared, _note("n-hoian", "old town fills by ten")],
        ]
    )
    service = _service([[] for _ in range(_SEGMENT_COUNT)])
    cmd = await _run(service, notes_service=notes_service)

    # One area read per STOP — a leg has no area of its own.
    assert notes_service.notes_for_area.await_count == 2
    note_ids = [n["id"] for n in _payload(cmd)["area_notes"]]
    # Pooled in trip order, the shared country-level claim said once.
    assert note_ids == ["n-shared", "n-hue", "n-hoian"]
