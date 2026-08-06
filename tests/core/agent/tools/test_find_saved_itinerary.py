"""`find_saved` on a multi-stop trip — one call fans out per segment (ADR-148)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage

from kebi.core.agent.location import ItineraryAnchor, WorkingLocation
from kebi.core.agent.tools.find_saved_tool import _run_find_saved
from kebi.core.config import get_config
from tests.core.agent.tools.test_find_saved import _full_result, _make_hit, _state

_ANCHORS = [
    ItineraryAnchor(
        name="Hanoi",
        lat=21.03,
        lng=105.85,
        city="Hanoi",
        country="Vietnam",
        country_code="vn",
    ),
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
# Stops in trip order, then the two legs — mirrors `itinerary_segments`.
_SEGMENT_COUNT = 5


def _itinerary_working_location() -> dict[str, Any]:
    return WorkingLocation(
        country="Vietnam",
        country_code="vn",
        city="Hanoi",
        lat=21.03,
        lng=105.85,
        search_radius_m=7000.0,
        scope_shape="itinerary",
        scope_tier="city",
        effective_mode="driving",
        itinerary=_ANCHORS,
    ).model_dump()


def _search(per_segment_hits: list[list[Any]]) -> MagicMock:
    service = MagicMock()
    service.search = AsyncMock(side_effect=per_segment_hits)
    return service


async def _run(service: MagicMock, **kw: Any) -> Any:
    return await _run_find_saved(
        hybrid_search=service,
        state=_state(working_location=_itinerary_working_location()),
        tool_call_id="tc-1",
        query=kw.pop("query", "places for the trip"),
        categories=None,
        tags=None,
        neighborhood=None,
        city=kw.pop("city", None),
        country=None,
        limit=kw.pop("limit", 10),
    )


async def test_one_call_searches_every_stop_and_leg() -> None:
    service = _search([[] for _ in range(_SEGMENT_COUNT)])
    await _run(service)
    assert service.search.await_count == _SEGMENT_COUNT
    # Each segment search is geofenced to its own point — the stop discs
    # first (Hanoi, Hue, Hoi An), then the leg midpoints.
    lats = [c.kwargs["filters"].lat for c in service.search.await_args_list]
    assert lats[:3] == [21.03, 16.46, 15.88]
    assert lats[3] == (21.03 + 16.46) / 2
    assert lats[4] == (16.46 + 15.88) / 2


async def test_segment_searches_use_the_per_segment_limit() -> None:
    service = _search([[] for _ in range(_SEGMENT_COUNT)])
    await _run(service, limit=25)
    per_segment = get_config().agent.itinerary.per_segment_limit
    assert all(c.kwargs["limit"] == per_segment for c in service.search.await_args_list)


async def test_candidates_carry_the_segment_they_belong_to() -> None:
    service = _search(
        [
            [],
            [_make_hit("Quan Hanh", "p-hue")],
            [],
            [],
            [_make_hit("Bep Hen", "p-danang")],
        ]
    )
    cmd = await _run(service)
    result = _full_result(cmd)
    by_name = {c.place.place_name: c.segment for c in result.candidates}
    assert by_name == {
        "Quan Hanh": "Hue",
        "Bep Hen": "on the way between Hue and Hoi An",
    }


async def test_a_place_in_a_stops_disc_is_that_stops_not_the_legs() -> None:
    """Stops search before legs, so a save found by both keeps its city's
    label — only a hit outside every stop stays "on the way"."""
    hue_hit = _make_hit("Quan Hanh", "p-hue")
    service = _search([[], [hue_hit], [], [hue_hit], []])
    cmd = await _run(service)
    result = _full_result(cmd)
    assert len(result.candidates) == 1
    assert result.candidates[0].segment == "Hue"


async def test_the_model_sees_the_segment_label() -> None:
    service = _search([[], [], [], [], [_make_hit("Bep Hen", "p-danang")]])
    cmd = await _run(service)
    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    view = json.loads(msg.content)
    assert view["candidates"][0]["segment"] == "on the way between Hue and Hoi An"


async def test_an_agent_named_area_wins_over_the_fan_out() -> None:
    """ "What did I save in Hue" is one city, not the whole trip — a named
    area falls back to the single-search path, exactly as it wins over the
    geofence on an area turn."""
    service = _search([[]])
    await _run(service, city="Hue")
    assert service.search.await_count == 1
    filters = service.search.await_args_list[0].kwargs["filters"]
    assert filters.city == "Hue"
    assert filters.lat is None


async def test_stop_duplicates_cannot_crowd_a_leg_save_off_the_end() -> None:
    """Regression (live): a leg's circle contains its end stops, so saves
    already attributed to the stops re-rank at the top of the leg search.
    At the bare per-segment limit they consumed every slot and the one save
    the leg exists to find — the Da Nang beach club on the Hue-Hoi An
    drive — ranked off the end and vanished. The leg search must ask for
    headroom equal to what has already been seen."""
    per_segment = get_config().agent.itinerary.per_segment_limit
    hanoi = _make_hit("Railway Tuan", "p-hanoi")
    hue = _make_hit("Quan Hanh", "p-hue")
    hoian_1 = _make_hit("Nu Eatery", "p-hoian-1")
    hoian_2 = _make_hit("Roving Chillhouse", "p-hoian-2")
    # The leg re-finds all four already-seen saves ahead of the two Da Nang
    # ones — exactly per_segment slots of duplicates at the old limit.
    leg_hits = [hue, hoian_1, hoian_2, hanoi][:per_segment] + [
        _make_hit("Bep Hen", "p-danang-1"),
        _make_hit("Kala Kala", "p-danang-2"),
    ]
    service = _search([[hanoi], [hue], [hoian_1, hoian_2], [], leg_hits])
    cmd = await _run(service)
    result = _full_result(cmd)
    by_name = {c.place.place_name: c.segment for c in result.candidates}
    assert by_name["Bep Hen"] == "on the way between Hue and Hoi An"
    assert by_name["Kala Kala"] == "on the way between Hue and Hoi An"
    # The leg call carried the headroom for its four inevitable repeats.
    assert service.search.await_args_list[4].kwargs["limit"] == per_segment + 4


async def test_empty_fan_out_reads_as_no_match() -> None:
    service = _search([[] for _ in range(_SEGMENT_COUNT)])
    cmd = await _run(service)
    assert _full_result(cmd).empty_reason == "no_match"
