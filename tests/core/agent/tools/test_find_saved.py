"""Tests for the `find_saved` agent tool.

Exercises the inner coroutine `_run_find_saved` directly so we can assert
the filter assembly + result packing logic without a real LLM or
LangGraph runtime. A separate test confirms the @tool-decorated wrapper
constructs cleanly and goes through `with_timeout`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools.consult_models import ConsultResult
from kebi.core.agent.tools.find_saved_tool import (
    _assemble_filters,
    _run_find_saved,
    build_find_saved_tool,
)
from kebi.core.places.models import (
    HybridSearchHit,
    PlaceCategory,
    PlaceCore,
    PlaceSource,
    UserPlace,
)


def _make_place(name: str = "Test Place", place_id: str = "p1") -> PlaceCore:
    return PlaceCore(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
    )


def _make_user_place(place_id: str = "p1") -> UserPlace:
    return UserPlace(
        user_place_id="up-1",
        user_id="user-1",
        place_id=place_id,
        source=PlaceSource.manual,
        saved_at=datetime.now(UTC),
    )


def _make_hit(name: str = "Test Place", place_id: str = "p1") -> HybridSearchHit:
    return HybridSearchHit(
        place=_make_place(name, place_id),
        user_data=_make_user_place(place_id),
        rrf_score=0.5,
        vector_rank=1,
        text_rank=1,
    )


def _state(
    *,
    working_location: dict[str, Any] | None = None,
    user_id: str = "user-1",
) -> dict[str, Any]:
    """Build a minimal AgentState-shaped dict for tool input."""
    return {
        "messages": [],
        "user_id": user_id,
        "taste_profile_summary": "",
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


def _bangkok_working_location() -> dict[str, Any]:
    return WorkingLocation(
        country="Thailand",
        city="Bangkok",
        lat=13.7563,
        lng=100.5018,
        density="dense",
        effective_mode="walking",
        scope_tier="walkable",
        scope_shape="area",
        search_radius_m=1000.0,
    ).model_dump()


# ---------------------------------------------------------------------------
# _assemble_filters
# ---------------------------------------------------------------------------


class TestAssembleFilters:
    def test_geofence_used_when_no_named_area(self) -> None:
        working = WorkingLocation(
            country="Thailand",
            city="Bangkok",
            lat=13.7,
            lng=100.5,
            search_radius_m=1500.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city=None,
            country=None,
            working=working,
        )
        assert f.lat == 13.7
        assert f.lng == 100.5
        assert f.radius_m == 1500
        assert f.city is None

    def test_named_area_drops_geofence(self) -> None:
        working = WorkingLocation(
            country="Thailand",
            city="Bangkok",
            lat=13.7,
            lng=100.5,
            search_radius_m=1500.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city="Chiang Mai",
            country=None,
            working=working,
        )
        assert f.lat is None
        assert f.lng is None
        assert f.radius_m is None
        assert f.city == "Chiang Mai"

    def test_no_working_no_named_area(self) -> None:
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city=None,
            country=None,
            working=None,
        )
        assert f.lat is None and f.lng is None and f.radius_m is None
        assert f.city is None

    def test_categories_and_tags_passed_through(self) -> None:
        f = _assemble_filters(
            categories=[PlaceCategory.restaurant, PlaceCategory.cafe],
            tags=["Thai", "outdoor_seating"],
            neighborhood=None,
            city=None,
            country=None,
            working=None,
        )
        assert f.categories == [PlaceCategory.restaurant, PlaceCategory.cafe]
        assert f.tags == ["Thai", "outdoor_seating"]

    def test_redundant_city_is_stripped_and_geofence_kept(self) -> None:
        """An agent that re-passes the working_location's own city must not
        suppress the geofence. Saves with NULL `places.city` (a Google
        quirk for island/rural Thailand addresses) would otherwise be
        unreachable — the geofence is what makes them findable."""
        working = WorkingLocation(
            country="Thailand",
            city="Koh Samui",
            lat=9.55,
            lng=100.04,
            search_radius_m=14000.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city="koh samui",  # lowercased + same as working — redundant
            country=None,
            working=working,
        )
        assert f.city is None
        assert f.lat == 9.55
        assert f.lng == 100.04
        assert f.radius_m == 14000

    def test_redundant_country_is_stripped(self) -> None:
        """Same logic for country — a redundant Thailand on a Thailand turn."""
        working = WorkingLocation(
            country="Thailand",
            city="Koh Samui",
            lat=9.55,
            lng=100.04,
            search_radius_m=14000.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city=None,
            country="Thailand",
            working=working,
        )
        assert f.country is None
        assert f.lat == 9.55

    def test_different_city_still_drops_geofence(self) -> None:
        """The redundancy stripper must NOT affect actual shifts — Chiang Mai
        from a Koh Samui working location is a real area override, not a
        redundancy. Geofence stays dropped; named filter stays applied."""
        working = WorkingLocation(
            country="Thailand",
            city="Koh Samui",
            lat=9.55,
            lng=100.04,
            search_radius_m=14000.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city="Chiang Mai",
            country=None,
            working=working,
        )
        assert f.city == "Chiang Mai"
        assert f.lat is None
        assert f.radius_m is None

    def test_zero_radius_is_dropped(self) -> None:
        """search_radius_m of 0.0 means resolver couldn't compute one."""
        working = WorkingLocation(
            country="Thailand",
            city="Bangkok",
            lat=13.7,
            lng=100.5,
            search_radius_m=0.0,
        )
        f = _assemble_filters(
            categories=None,
            tags=None,
            neighborhood=None,
            city=None,
            country=None,
            working=working,
        )
        # lat/lng set without radius would be rejected by HybridSearchFilters
        # validation; the assembler drops them as a pair instead.
        assert f.radius_m is None


# ---------------------------------------------------------------------------
# _run_find_saved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_candidates() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[_make_hit("A"), _make_hit("B", "p2")])

    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=_bangkok_working_location()),
        tool_call_id="tc-1",
        query="ramen",
        categories=[PlaceCategory.restaurant],
        tags=None,
        neighborhood=None,
        city=None,
        country=None,
        limit=5,
    )

    assert isinstance(cmd, Command)
    msgs = cmd.update["messages"]
    assert len(msgs) == 1
    tool_msg = msgs[0]
    assert isinstance(tool_msg, ToolMessage)
    payload = ConsultResult.model_validate_json(tool_msg.content)
    assert len(payload.candidates) == 2
    assert payload.empty_reason is None
    assert all(c.source == "saved" for c in payload.candidates)
    assert cmd.update["tool_calls_used"] == 1

    # Verify the filter that was sent to hybrid_search
    call = hybrid.search.await_args
    filters = call.kwargs["filters"]
    assert filters.categories == [PlaceCategory.restaurant]
    assert filters.lat == 13.7563
    assert filters.radius_m == 1000
    assert call.kwargs["limit"] == 5
    assert call.kwargs["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_empty_results_with_location_returns_no_match() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[])

    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=_bangkok_working_location()),
        tool_call_id="tc-1",
        query="moroccan",
        categories=None,
        tags=None,
        neighborhood=None,
        city=None,
        country=None,
        limit=10,
    )
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.candidates == []
    assert payload.empty_reason == "no_match"


@pytest.mark.asyncio
async def test_empty_results_no_location_no_named_area() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[])

    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=None),
        tool_call_id="tc-1",
        query="anything",
        categories=None,
        tags=None,
        neighborhood=None,
        city=None,
        country=None,
        limit=10,
    )
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_location"


@pytest.mark.asyncio
async def test_empty_results_no_location_with_named_area_is_no_match() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[])

    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=None),
        tool_call_id="tc-1",
        query="brunch",
        categories=None,
        tags=None,
        neighborhood=None,
        city="Chiang Mai",
        country=None,
        limit=10,
    )
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"


@pytest.mark.asyncio
async def test_agent_tags_pass_through_to_filter() -> None:
    """Agent-supplied tags (incl. agent-translated hard constraints) reach SQL."""
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[])

    await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=_bangkok_working_location()),
        tool_call_id="tc-1",
        query="dinner",
        categories=[PlaceCategory.restaurant],
        tags=["Thai", "vegetarian", "outdoor_seating"],
        neighborhood=None,
        city=None,
        country=None,
        limit=10,
    )
    filters = hybrid.search.await_args.kwargs["filters"]
    assert filters.tags == ["Thai", "vegetarian", "outdoor_seating"]


@pytest.mark.asyncio
async def test_reasoning_step_summary_emitted() -> None:
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[_make_hit("X")])

    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=_bangkok_working_location()),
        tool_call_id="tc-1",
        query="ramen",
        categories=None,
        tags=None,
        neighborhood=None,
        city=None,
        country=None,
        limit=10,
    )
    steps = cmd.update["reasoning_steps"]
    assert len(steps) == 1
    assert steps[0].step == "find_saved.summary"
    assert steps[0].source == "agent"
    assert "ramen" in steps[0].summary
    assert "1 saved match" in steps[0].summary
    # Singular: no "es" suffix, just "match".
    assert "matches" not in steps[0].summary
    # Names of the matched places are surfaced in the summary.
    assert "X" in steps[0].summary


@pytest.mark.asyncio
async def test_reasoning_step_summary_lists_names_with_correct_plural() -> None:
    """Multi-result summary previews up to 3 names and uses 'matches' plural.

    Regression for the user-visible step which used to read "3 saved matchs"
    (wrong plural) and didn't surface names — so users had to dig into
    `tool_results` to see what was actually returned.
    """
    hybrid = MagicMock()
    hybrid.search = AsyncMock(
        return_value=[
            _make_hit("Wat Phra Yai", "p1"),
            _make_hit("Samui Elephant Sanctuary", "p2"),
            _make_hit("Silver Beach", "p3"),
            _make_hit("Na Muang Waterfall 1", "p4"),
        ]
    )
    cmd = await _run_find_saved(
        hybrid_search=hybrid,
        state=_state(working_location=_bangkok_working_location()),
        tool_call_id="tc-1",
        query="famous places koh samui",
        categories=None,
        tags=None,
        neighborhood=None,
        city=None,
        country=None,
        limit=10,
    )
    summary = cmd.update["reasoning_steps"][0].summary
    assert "4 saved matches" in summary
    assert "matchs" not in summary
    # First 3 names previewed, 4th hinted at via "and a few more".
    assert "Wat Phra Yai" in summary
    assert "Samui Elephant Sanctuary" in summary
    assert "Silver Beach" in summary
    assert "Na Muang Waterfall 1" not in summary
    assert "and a few more" in summary


# ---------------------------------------------------------------------------
# build_find_saved_tool + with_timeout integration
# ---------------------------------------------------------------------------


def test_tool_factory_constructs_with_expected_schema() -> None:
    """The @tool wrapper exposes the right name and the agent-facing args.

    InjectedState + InjectedToolCallId are stripped from the args_schema
    LangChain shows the LLM, so the schema should only carry the 7
    user-facing fields.
    """
    hybrid = MagicMock()
    hybrid.search = AsyncMock(return_value=[])

    tool = build_find_saved_tool(hybrid)
    assert tool.name == "find_saved"

    # `tool_call_schema` is what the LLM sees — InjectedState +
    # InjectedToolCallId are stripped here while remaining in the
    # underlying `args_schema` (LangChain handles injection at runtime).
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
async def test_tool_degrades_on_exception() -> None:
    """A raising hybrid_search yields a degraded Command via with_timeout."""
    from kebi.core.agent.tools._with_timeout import with_timeout

    async def _boom() -> Command[Any]:
        raise RuntimeError("simulated failure")

    cmd = await with_timeout(
        tool_name="find_saved",
        tool_call_id="tc-err",
        seconds=5,
        state=_state(),
        coro=_boom(),
    )
    msg = cmd.update["messages"][0]
    payload = json.loads(msg.content)
    assert payload["empty_reason"] == "error"
    assert cmd.update["error_count"] == 1
    assert cmd.update["tool_calls_used"] == 1
    assert cmd.update["reasoning_steps"][-1].step == "find_saved.failure"
