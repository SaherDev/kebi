"""Tests for the `suggest_places` agent tool.

Exercises `_run_suggest_places` directly with stubbed
`CandidateNamerService` and `PlacesSearchService`. Asserts:

- Location anchoring: no working_location → no namer / no provider call.
- Namer-empty → no_match.
- Provider misses → drop, all-miss → no_match.
- Post-fetch tag filter drops constraint-violating places.
- Reasoning steps narrate every phase in plain language and in order.
- Result envelope: `source="suggested"`, `reason` populated, limit respected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools._hard_constraints import hard_constraints_satisfied
from kebi.core.agent.tools.candidate_namer import CandidateName, CandidateNames
from kebi.core.agent.tools.consult_models import ConsultResult
from kebi.core.agent.tools.suggest_places_tool import (
    _run_suggest_places,
    build_suggest_places_tool,
)
from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceTag,
)
from kebi.core.places.tags import DietaryTag, TagType


def _bangkok_working() -> dict[str, Any]:
    return WorkingLocation(
        country="Thailand",
        city="Bangkok",
        neighborhood="Sukhumvit",
        lat=13.7563,
        lng=100.5018,
        density="dense",
        effective_mode="walking",
        scope_tier="walkable",
        scope_shape="area",
        search_radius_m=1200.0,
    ).model_dump()


def _bangkok_no_radius() -> dict[str, Any]:
    """Working location with zero radius — location wasn't fully anchored."""
    return WorkingLocation(
        country="Thailand",
        city="Bangkok",
        lat=13.7563,
        lng=100.5018,
        search_radius_m=0.0,
    ).model_dump()


def _state(
    *,
    working_location: dict[str, Any] | None = None,
    taste_summary: str = "",
    user_id: str = "user-1",
) -> dict[str, Any]:
    return {
        "messages": [],
        "user_id": user_id,
        "taste_profile_summary": taste_summary,
        "memory_summary": "",
        "user_location": None,
        "working_location": working_location,
        "location_clarification": None,
        "movement_profile": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


def _place(
    name: str,
    *,
    place_id: str,
    tags: list[PlaceTag] | None = None,
) -> PlaceObject:
    return PlaceObject(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
        tags=tags or [],
        cached_at=datetime.now(UTC),
    )


def _veg_tag() -> PlaceTag:
    return PlaceTag(
        type=TagType.dietary,
        value=DietaryTag.vegetarian,
        source="google",
    )


def _make_namer(candidates: list[CandidateName]) -> MagicMock:
    """Stub CandidateNamerService — bypasses prompt rendering entirely."""
    namer = MagicMock()
    namer.generate = AsyncMock(return_value=CandidateNames(candidates=candidates))
    return namer


def _make_search_factory(
    *, by_name: dict[str, list[PlaceObject]]
) -> tuple[
    Callable[[], AbstractAsyncContextManager[MagicMock]],
    MagicMock,
]:
    """Stub the per-task PlacesSearchService factory.

    Each `factory()` call yields an async context manager whose value
    is the shared `search` mock — so the production code's
    `async with factory() as svc` pattern works, and tests can still
    assert on `search.find.await_args_list` to inspect calls across
    all parallel fan-outs. The mock's `.find` is routed by
    `place_names[0]` so each candidate name maps to a canned hit list.
    """
    search = MagicMock()

    async def _find(query: Any, limit: int = 1) -> list[PlaceObject]:
        names = query.place_names or []
        if not names:
            return []
        return by_name.get(names[0], [])

    search.find = AsyncMock(side_effect=_find)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[MagicMock]:
        yield search

    return _factory, search


# ---------------------------------------------------------------------------
# hard_constraints_satisfied
# ---------------------------------------------------------------------------


class TestHardConstraintFilter:
    def test_empty_required_always_passes(self) -> None:
        place = _place("X", place_id="p1")
        assert hard_constraints_satisfied(place, []) is True

    def test_all_required_present_passes(self) -> None:
        place = _place("X", place_id="p1", tags=[_veg_tag()])
        assert hard_constraints_satisfied(place, ["vegetarian"]) is True

    def test_missing_one_required_fails(self) -> None:
        place = _place("X", place_id="p1", tags=[_veg_tag()])
        assert hard_constraints_satisfied(place, ["vegetarian", "halal"]) is False

    def test_case_insensitive(self) -> None:
        place = _place("X", place_id="p1", tags=[_veg_tag()])
        assert hard_constraints_satisfied(place, ["VEGETARIAN"]) is True


# ---------------------------------------------------------------------------
# _run_suggest_places — location anchoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_working_location_returns_no_location_no_calls() -> None:
    """Hard precondition: no anchor → no namer, no provider, single step."""
    namer = _make_namer([])
    factory, search = _make_search_factory(by_name={})

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=None),
        tool_call_id="tc-1",
        query="famous spots",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    assert isinstance(cmd, Command)
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_location"
    assert payload.candidates == []
    namer.generate.assert_not_called()
    search.find.assert_not_called()
    steps = cmd.update["reasoning_steps"]
    assert len(steps) == 1
    assert steps[0].step == "suggest_places.no_location"


@pytest.mark.asyncio
async def test_zero_radius_treated_as_no_location() -> None:
    """A WorkingLocation with radius 0 fails the anchor gate too."""
    namer = _make_namer([])
    factory, search = _make_search_factory(by_name={})

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_no_radius()),
        tool_call_id="tc-1",
        query="famous spots",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_location"
    namer.generate.assert_not_called()
    search.find.assert_not_called()


# ---------------------------------------------------------------------------
# _run_suggest_places — empty paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namer_empty_returns_no_match() -> None:
    namer = _make_namer([])
    factory, search = _make_search_factory(by_name={})

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="something niche",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"
    search.find.assert_not_called()
    steps = cmd.update["reasoning_steps"]
    step_ids = [s.step for s in steps]
    assert step_ids == ["suggest_places.locate", "suggest_places.namer_empty"]


@pytest.mark.asyncio
async def test_provider_all_misses_returns_no_match() -> None:
    """Namer suggests names but provider returns nothing for any of them."""
    namer = _make_namer(
        [
            CandidateName(name="Made-up Spot", reason="r1"),
            CandidateName(name="Another Ghost", reason="r2"),
        ]
    )
    factory, search = _make_search_factory(by_name={})  # every name → empty hits

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="anything",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids == [
        "suggest_places.locate",
        "suggest_places.brainstorm",
        "suggest_places.no_provider_hits",
    ]
    assert search.find.await_count == 2


@pytest.mark.asyncio
async def test_constraint_filter_drops_everything_returns_no_match() -> None:
    """All places validated, but none carry the required dietary tag."""
    namer = _make_namer(
        [
            CandidateName(name="Steakhouse 1", reason="r1"),
            CandidateName(name="Steakhouse 2", reason="r2"),
        ]
    )
    by_name = {
        "Steakhouse 1": [_place("Steakhouse 1", place_id="p1")],
        "Steakhouse 2": [_place("Steakhouse 2", place_id="p2")],
    }
    factory, search = _make_search_factory(by_name=by_name)

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="dinner",
        categories=None,
        tags=["vegetarian"],
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids[-1] == "suggest_places.constraints_drop"


# ---------------------------------------------------------------------------
# _run_suggest_places — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_suggested_candidates_with_reasons() -> None:
    namer = _make_namer(
        [
            CandidateName(name="Gaa", reason="acclaimed plant-forward tasting"),
            CandidateName(name="Bo.Lan", reason="seasonal Thai, vegetarian set"),
        ]
    )
    by_name = {
        "Gaa": [_place("Gaa", place_id="p1", tags=[_veg_tag()])],
        "Bo.Lan": [_place("Bo.Lan", place_id="p2", tags=[_veg_tag()])],
    }
    factory, search = _make_search_factory(by_name=by_name)

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="vegetarian dinner",
        categories=[PlaceCategory.restaurant],
        tags=["vegetarian"],
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason is None
    assert len(payload.candidates) == 2
    for c in payload.candidates:
        assert c.source == "suggested"
        assert c.reason  # non-empty
        assert c.user_data is None
    names = [c.place.place_name for c in payload.candidates]
    assert names == ["Gaa", "Bo.Lan"]  # namer order preserved
    reasons = [c.reason for c in payload.candidates]
    assert reasons == [
        "acclaimed plant-forward tasting",
        "seasonal Thai, vegetarian set",
    ]

    # Reasoning steps walk all four phases in order.
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids == [
        "suggest_places.locate",
        "suggest_places.brainstorm",
        "suggest_places.summary",
    ]
    summary = cmd.update["reasoning_steps"][-1].summary
    assert "Gaa" in summary and "Bo.Lan" in summary


@pytest.mark.asyncio
async def test_limit_caps_returned_candidates() -> None:
    """Namer + provider produce more than `limit` — tool caps at limit."""
    proposed = [CandidateName(name=f"Place {i}", reason=f"r{i}") for i in range(6)]
    by_name = {f"Place {i}": [_place(f"Place {i}", place_id=f"p{i}")] for i in range(6)}
    namer = _make_namer(proposed)
    factory, search = _make_search_factory(by_name=by_name)

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="anything",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=3,
        name_count=8,
        concurrency=5,
    )
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert len(payload.candidates) == 3
    assert [c.place.place_name for c in payload.candidates] == [
        "Place 0",
        "Place 1",
        "Place 2",
    ]


@pytest.mark.asyncio
async def test_provider_calls_carry_location_context() -> None:
    """Every `find()` call MUST be bound to the working location's circle."""
    namer = _make_namer([CandidateName(name="Gaa", reason="r")])
    by_name = {"Gaa": [_place("Gaa", place_id="p1")]}
    factory, search = _make_search_factory(by_name=by_name)

    await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="anything",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )
    # Inspect every find() call we made.
    for call in search.find.await_args_list:
        place_query = call.args[0] if call.args else call.kwargs["query"]
        loc: LocationContext | None = place_query.location
        assert loc is not None
        assert loc.lat == 13.7563
        assert loc.lng == 100.5018
        assert loc.radius_m == 1200
        assert loc.city == "Bangkok"


# ---------------------------------------------------------------------------
# build_suggest_places_tool — factory / schema
# ---------------------------------------------------------------------------


def test_tool_factory_exposes_same_arg_schema_as_find_saved() -> None:
    """Byte-identical arg surface keeps the agent prompt small."""
    namer = _make_namer([])
    factory, search = _make_search_factory(by_name={})

    tool = build_suggest_places_tool(namer, factory)
    assert tool.name == "suggest_places"

    schema_fields = set(tool.tool_call_schema.model_fields.keys())
    assert schema_fields == {
        "query",
        "categories",
        "tags",
        "neighborhood",
        "city",
        "country",
        "limit",
    }


@pytest.mark.asyncio
async def test_tool_degrades_on_namer_exception() -> None:
    """A raising namer/provider yields a degraded Command via with_timeout."""
    from kebi.core.agent.tools._with_timeout import with_timeout

    async def _boom() -> Command[Any]:
        raise RuntimeError("simulated tool failure")

    cmd = await with_timeout(
        tool_name="suggest_places",
        tool_call_id="tc-err",
        seconds=5,
        state=_state(working_location=_bangkok_working()),
        coro=_boom(),
    )
    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    import json as _json

    payload = _json.loads(msg.content)
    assert payload["empty_reason"] == "error"
    assert cmd.update["error_count"] == 1
    assert cmd.update["tool_calls_used"] == 1
    assert cmd.update["reasoning_steps"][-1].step == "suggest_places.failure"
