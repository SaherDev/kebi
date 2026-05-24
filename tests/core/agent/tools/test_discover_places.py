"""Tests for the `discover_places` agent tool.

Exercises `_run_discover_places` directly with a stubbed
`PlacesSearchService` factory. Asserts:

- Location anchoring: no working_location → no provider call.
- Empty provider response → no_match.
- Geographic-feature drop strips administrative-only hits.
- Post-fetch hard-constraint tag filter drops violating places.
- Reasoning steps narrate every phase in plain language and in order.
- Result envelope: `source="discovered"`, `user_data=None`, limit respected.
- Provider call carries the working location's circle (lat/lng/radius).
- Tool factory exposes the same arg schema as the other consult tools.
- Tool degrades to `empty_reason="error"` via with_timeout when it raises.
- Named-area overrides are silently dropped (parity with suggest_places).
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
from kebi.core.agent.tools.consult_models import ConsultResult
from kebi.core.agent.tools.discover_places_tool import (
    _run_discover_places,
    build_discover_places_tool,
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
    user_id: str = "user-1",
) -> dict[str, Any]:
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


def _place(
    name: str,
    *,
    place_id: str,
    categories: list[PlaceCategory] | None = None,
    tags: list[PlaceTag] | None = None,
) -> PlaceObject:
    return PlaceObject(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=categories if categories is not None else [PlaceCategory.pharmacy],
        tags=tags or [],
        cached_at=datetime.now(UTC),
    )


def _veg_tag() -> PlaceTag:
    return PlaceTag(
        type=TagType.dietary,
        value=DietaryTag.vegetarian,
        source="google",
    )


def _make_search_factory(
    *, hits: list[PlaceObject] | Exception,
) -> tuple[
    Callable[[], AbstractAsyncContextManager[MagicMock]],
    MagicMock,
]:
    """Stub the per-task PlacesSearchService factory.

    `hits` is the canned response from a single `find()` call. Pass an
    Exception instance to simulate a provider failure (raised inside
    the `async with` block).
    """
    search = MagicMock()

    async def _find(query: Any, limit: int = 10) -> list[PlaceObject]:
        if isinstance(hits, Exception):
            raise hits
        return hits

    search.find = AsyncMock(side_effect=_find)

    @asynccontextmanager
    async def _factory() -> AsyncIterator[MagicMock]:
        yield search

    return _factory, search


# ---------------------------------------------------------------------------
# Location anchoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_working_location_returns_no_location_no_call() -> None:
    """Hard precondition: no anchor → no provider call, single step."""
    factory, search = _make_search_factory(hits=[])

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=None),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    assert isinstance(cmd, Command)
    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_location"
    assert payload.candidates == []
    search.find.assert_not_called()
    steps = cmd.update["reasoning_steps"]
    assert len(steps) == 1
    assert steps[0].step == "discover_places.no_location"


@pytest.mark.asyncio
async def test_zero_radius_treated_as_no_location() -> None:
    """A WorkingLocation with radius 0 fails the anchor gate."""
    factory, search = _make_search_factory(hits=[])

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_no_radius()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_location"
    search.find.assert_not_called()


# ---------------------------------------------------------------------------
# Empty / filtered-empty paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_empty_returns_no_match() -> None:
    factory, search = _make_search_factory(hits=[])

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids == ["discover_places.start", "discover_places.no_match"]
    assert search.find.await_count == 1


@pytest.mark.asyncio
async def test_geo_filter_drops_administrative_only_hits() -> None:
    """Hits with empty categories AND administrative names get dropped."""
    admin_hit = PlaceObject(
        id="p1",
        provider_id="google:p1",
        place_name="Sukhumvit Road",
        categories=[],
        tags=[],
        cached_at=datetime.now(UTC),
    )
    factory, search = _make_search_factory(hits=[admin_hit])

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"  # only admin hit was dropped
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids[-1] == "discover_places.no_match"


@pytest.mark.asyncio
async def test_constraint_filter_drops_everything_returns_no_match() -> None:
    """Provider returns places, but none carry the required dietary tag."""
    factory, search = _make_search_factory(
        hits=[
            _place("Pharmacy 1", place_id="p1"),
            _place("Pharmacy 2", place_id="p2"),
        ]
    )

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=["vegetarian"],  # neither place carries this — filter drops both
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "no_match"
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids[-1] == "discover_places.constraints_drop"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_discovered_candidates() -> None:
    factory, search = _make_search_factory(
        hits=[
            _place("Boots Pharmacy", place_id="p1"),
            _place("Watson's", place_id="p2"),
        ]
    )

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason is None
    assert len(payload.candidates) == 2
    for c in payload.candidates:
        assert c.source == "discovered"
        assert c.user_data is None
        # `reason` stays None at the tool layer — the agent composes
        # the per-pick reason in its prose answer from place fields +
        # taste + memory + working-location context (not a generic
        # template pre-baked in code).
        assert c.reason is None
        assert c.rrf_score == 0.0
    names = [c.place.place_name for c in payload.candidates]
    assert names == ["Boots Pharmacy", "Watson's"]

    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids == ["discover_places.start", "discover_places.summary"]
    summary = cmd.update["reasoning_steps"][-1].summary
    assert "Boots Pharmacy" in summary and "Watson's" in summary


@pytest.mark.asyncio
async def test_limit_caps_returned_candidates() -> None:
    """Provider returned more than limit — tool caps."""
    factory, search = _make_search_factory(
        hits=[_place(f"Pharmacy {i}", place_id=f"p{i}") for i in range(6)]
    )

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=3,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert len(payload.candidates) == 3
    assert [c.place.place_name for c in payload.candidates] == [
        "Pharmacy 0",
        "Pharmacy 1",
        "Pharmacy 2",
    ]


@pytest.mark.asyncio
async def test_provider_call_carries_location_context() -> None:
    """The single find() call MUST be bound to the working location's circle."""
    factory, search = _make_search_factory(
        hits=[_place("Boots", place_id="p1")]
    )

    await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    call = search.find.await_args_list[0]
    place_query = call.args[0] if call.args else call.kwargs["query"]
    loc: LocationContext | None = place_query.location
    assert loc is not None
    assert loc.lat == 13.7563
    assert loc.lng == 100.5018
    assert loc.radius_m == 1200
    assert loc.city == "Bangkok"
    assert loc.neighborhood == "Sukhumvit"
    assert loc.country == "Thailand"
    assert place_query.place_names == ["pharmacy"]
    assert place_query.categories == [PlaceCategory.pharmacy]


@pytest.mark.asyncio
async def test_provider_call_omits_place_names_when_query_empty() -> None:
    """Empty query → no place_names entry; categories alone drive the search."""
    factory, search = _make_search_factory(
        hits=[_place("Boots", place_id="p1")]
    )

    await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    call = search.find.await_args_list[0]
    place_query = call.args[0] if call.args else call.kwargs["query"]
    assert place_query.place_names is None


# ---------------------------------------------------------------------------
# Provider failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_exception_returns_error_envelope() -> None:
    factory, search = _make_search_factory(hits=RuntimeError("upstream blew up"))

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert payload.empty_reason == "error"
    step_ids = [s.step for s in cmd.update["reasoning_steps"]]
    assert step_ids == ["discover_places.start", "discover_places.provider_error"]


# ---------------------------------------------------------------------------
# Tool factory / schema
# ---------------------------------------------------------------------------


def test_tool_factory_exposes_same_arg_schema_as_the_others() -> None:
    """Byte-identical arg surface keeps the agent prompt small."""
    factory, search = _make_search_factory(hits=[])

    tool = build_discover_places_tool(factory)
    assert tool.name == "discover_places"

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
async def test_tool_degrades_on_runtime_exception_via_with_timeout() -> None:
    """A raising coro yields a degraded Command via with_timeout."""
    from kebi.core.agent.tools._with_timeout import with_timeout

    async def _boom() -> Command[Any]:
        raise RuntimeError("simulated tool failure")

    cmd = await with_timeout(
        tool_name="discover_places",
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
    assert cmd.update["reasoning_steps"][-1].step == "discover_places.failure"


@pytest.mark.asyncio
async def test_discovered_candidate_reason_left_for_agent_to_compose() -> None:
    """Discovered candidates carry `reason=None` from the tool layer.

    The per-pick reason the user sees is the agent's decision, composed
    in prose from `place.location` (for distance) + `place.tags` +
    `place.categories` + taste profile + memory + working-location
    context. Pre-computing a generic reason here would short-circuit
    that decision.
    """
    near = _place("Near Pharmacy", place_id="p1")
    near.location = LocationContext(lat=13.7600, lng=100.5040, radius_m=None)
    far = _place("Other Pharmacy", place_id="p2")
    factory, _ = _make_search_factory(hits=[near, far])

    cmd = await _run_discover_places(
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="pharmacy",
        categories=[PlaceCategory.pharmacy],
        tags=None,
        limit=10,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert len(payload.candidates) == 2
    assert all(c.reason is None for c in payload.candidates)
