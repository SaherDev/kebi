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

from kebi.core.agent.location import CorridorPath, CorridorTarget, WorkingLocation
from kebi.core.agent.tools._hard_constraints import hard_constraints_satisfied
from kebi.core.agent.tools.candidate_namer import CandidateName, CandidateNames
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.agent.tools.suggest_places_tool import (
    _best_name_match,
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
from kebi.core.utils.geo import haversine_m


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


def _bangkok_city_working() -> dict[str, Any]:
    """City-scope, transit, dense — a broad ~9.8 km radius (the utility bug)."""
    return WorkingLocation(
        country="Thailand",
        city="Bangkok",
        lat=13.7563,
        lng=100.5018,
        density="dense",
        effective_mode="transit",
        scope_tier="city",
        scope_shape="area",
        search_radius_m=9800.0,
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
# _run_suggest_places — candidate dedup before the provider fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_named_candidates_issue_one_provider_call() -> None:
    """Two namer phrasings that normalize to the same name must fan out to a
    SINGLE billed provider lookup, keeping the first phrasing + reason."""
    namer = _make_namer(
        [
            CandidateName(name="Wat Pho", reason="first phrasing"),
            CandidateName(name="wat pho!!", reason="second phrasing"),
        ]
    )
    by_name = {"Wat Pho": [_place("Wat Pho", place_id="p1")]}
    factory, search = _make_search_factory(by_name=by_name)

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="temple",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    # One distinct name → one provider call (no duplicate billing), and the
    # call carries the first phrasing the namer emitted.
    assert search.find.await_count == 1
    assert search.find.await_args_list[0].args[0].place_names == ["Wat Pho"]

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert len(payload.candidates) == 1
    assert payload.candidates[0].reason == "first phrasing"


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

    # Internal phases stream for tracing, in order; the user sees one row.
    steps = cmd.update["reasoning_steps"]
    step_ids = [s.step for s in steps]
    assert step_ids == [
        "suggest_places.locate",
        "suggest_places.brainstorm",
        "suggest_places.summary",
    ]
    # Collapsed to one user-visible row (ADR-103): locate/brainstorm are debug,
    # the outcome is the single user row carrying the tool's action title.
    user_steps = [s for s in steps if s.visibility == "user"]
    assert len(user_steps) == 1
    assert user_steps[0].step == "suggest_places.summary"
    assert user_steps[0].title == "suggested a few spots"
    summary = user_steps[0].summary
    assert "Gaa" in summary and "Bo.Lan" in summary


# ---------------------------------------------------------------------------
# distance ordering — brand/chain resolves to the nearest branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_requests_distance_ordering() -> None:
    """Every provider lookup asks for nearest-first so a chain resolves to
    its closest branch, not the prominent flagship."""
    namer = _make_namer([CandidateName(name="Sumitomo Mitsui Bank", reason="r")])
    by_name = {"Sumitomo Mitsui Bank": [_place("SMBC", place_id="p1")]}
    factory, search = _make_search_factory(by_name=by_name)

    await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="ATM",
        categories=[PlaceCategory.atm],
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=1,
        name_count=8,
        concurrency=5,
    )

    assert search.find.await_count == 1
    for call in search.find.await_args_list:
        assert call.args[0].sort_by == "distance"


@pytest.mark.asyncio
async def test_takes_nearest_branch_from_ordered_results() -> None:
    """Provider returns branches nearest-first (distance sort); the tool
    surfaces the first one — the nearest, not the far flagship."""
    namer = _make_namer([CandidateName(name="Chain Bank", reason="reliable")])
    nearest = _place("Chain Bank — Shinjuku", place_id="near")
    flagship = _place("Chain Bank — Flagship", place_id="far")
    by_name = {"Chain Bank": [nearest, flagship]}  # distance-ordered
    factory, _ = _make_search_factory(by_name=by_name)

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="bank",
        categories=[PlaceCategory.bank],
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=1,
        name_count=8,
        concurrency=5,
    )

    payload = ConsultResult.model_validate_json(cmd.update["messages"][0].content)
    assert len(payload.candidates) == 1
    assert payload.candidates[0].place.place_name == "Chain Bank — Shinjuku"


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


@pytest.mark.asyncio
async def test_utility_category_clamps_provider_radius() -> None:
    """A utility errand on a broad city-scope turn searches a walkable circle."""
    namer = _make_namer([CandidateName(name="Some Bank", reason="r")])
    by_name = {"Some Bank": [_place("Some Bank", place_id="p1")]}
    factory, search = _make_search_factory(by_name=by_name)

    await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_city_working()),
        tool_call_id="tc-1",
        query="ATM",
        categories=[PlaceCategory.atm],
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=1,
        name_count=8,
        concurrency=5,
    )
    for call in search.find.await_args_list:
        loc: LocationContext | None = call.args[0].location
        assert loc is not None
        # walkable(1000) × transit(2.0) × dense(0.7) = 1400, down from 9800.
        assert loc.radius_m == 1400


@pytest.mark.asyncio
async def test_non_utility_category_keeps_broad_radius() -> None:
    """A non-errand intent is untouched by the utility clamp."""
    namer = _make_namer([CandidateName(name="Gaa", reason="r")])
    by_name = {"Gaa": [_place("Gaa", place_id="p1")]}
    factory, search = _make_search_factory(by_name=by_name)

    await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_city_working()),
        tool_call_id="tc-1",
        query="dinner",
        categories=[PlaceCategory.restaurant],
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=1,
        name_count=8,
        concurrency=5,
    )
    for call in search.find.await_args_list:
        loc: LocationContext | None = call.args[0].location
        assert loc is not None
        assert loc.radius_m == 9800


# ---------------------------------------------------------------------------
# build_suggest_places_tool — factory / schema
# ---------------------------------------------------------------------------


def test_tool_factory_shares_the_find_saved_arg_surface_plus_place_names() -> None:
    """The shared arg surface keeps the agent prompt small; `place_names` is
    the one deliberate divergence (ADR-137).

    `suggest_places` is the propose-and-validate tool, so it is the only one
    the agent can hand its own known place names to. `find_saved` searches a
    corpus the user already owns and `discover_places` asks the provider
    directly — neither has anything to validate on the agent's behalf. The
    extra arg is also a useful signal to the agent about which tool to reach
    for when it already knows the area.
    """
    namer = _make_namer([])
    factory, search = _make_search_factory(by_name={})

    tool = build_suggest_places_tool(namer, factory)
    assert tool.name == "suggest_places"

    schema_fields = set(tool.tool_call_schema.model_fields.keys())
    assert schema_fields == {
        "query",
        "place_names",
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


# ---------------------------------------------------------------------------
# ADR-117: namer icon rides the validation query; warm rows stamped for display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namer_icon_rides_query_as_icon_hint() -> None:
    namer = _make_namer(
        [CandidateName(name="Dubai Fountain", reason="the show", icon="⛲")]
    )
    factory, search = _make_search_factory(
        by_name={"Dubai Fountain": [_place("Dubai Fountain", place_id="p1")]}
    )

    await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="fountain show",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )

    query = search.find.await_args_list[0].args[0]
    assert query.icon_hint == "⛲"


@pytest.mark.asyncio
async def test_warm_row_without_icon_stamped_for_display_only() -> None:
    # The hit already exists in the catalog with icon=None (warm path —
    # icon_hint never fires). The response candidate still shows the
    # namer's pick; no write happens through the tool.
    namer = _make_namer(
        [CandidateName(name="Gaa", reason="tasting menu", icon="🌿")]
    )
    factory, _search = _make_search_factory(
        by_name={"Gaa": [_place("Gaa", place_id="p1")]}
    )

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="dinner",
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
    assert payload.candidates[0].place.icon == "🌿"


@pytest.mark.asyncio
async def test_stored_icon_wins_over_namer_icon() -> None:
    namer = _make_namer(
        [CandidateName(name="Gaa", reason="tasting menu", icon="🌿")]
    )
    stored = _place("Gaa", place_id="p1").model_copy(update={"icon": "🍽️"})
    factory, _search = _make_search_factory(by_name={"Gaa": [stored]})

    cmd = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=_bangkok_working()),
        tool_call_id="tc-1",
        query="dinner",
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
    assert payload.candidates[0].place.icon == "🍽️"


# ---------------------------------------------------------------------------
# Route-shaped turns (ADR-136)
# ---------------------------------------------------------------------------

_DA_NANG = (16.0544, 108.2022)
_HUE = (16.4637, 107.5909)
_HOI_AN = (15.8801, 108.3380)
_SAIGON = (10.8231, 106.6297)
_HANOI = (21.0278, 105.8342)
_LANG_CO = (16.2333, 108.0667)


def _route_working(
    *,
    stops: list[tuple[str, tuple[float, float]]],
    origin: tuple[float, float] = _DA_NANG,
    city: str = "Da Nang",
) -> dict[str, Any]:
    return WorkingLocation(
        country="Vietnam",
        city=city,
        lat=origin[0],
        lng=origin[1],
        country_code="vn",
        effective_mode="driving",
        scope_tier="city",
        scope_shape="corridor",
        search_radius_m=9_000.0,
        corridor=CorridorPath(
            stops=[
                CorridorTarget(name=name, lat=p[0], lng=p[1]) for name, p in stops
            ]
        ),
    ).model_dump()


def _located(name: str, *, place_id: str, point: tuple[float, float]) -> PlaceObject:
    return PlaceObject(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
        location=LocationContext(lat=point[0], lng=point[1]),
        cached_at=datetime.now(UTC),
    )


async def _run_route(
    *, working: dict[str, Any], by_name: dict[str, list[PlaceObject]]
) -> tuple[ConsultResult, MagicMock, MagicMock]:
    namer = _make_namer(
        [CandidateName(name=n, reason="on the way") for n in by_name]
    )
    factory, search = _make_search_factory(by_name=by_name)
    command = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=working),
        tool_call_id="call-route",
        query="somewhere to stop",
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=5,
        name_count=8,
        concurrency=5,
    )
    message = command.update["messages"][0]
    assert isinstance(message, ToolMessage)
    return ConsultResult.model_validate_json(str(message.content)), namer, search


class TestRouteShapedSuggest:
    async def test_results_are_ordered_along_the_route(self) -> None:
        """The done-when: real stops, in journey order, not a ranked list."""
        result, _, _ = await _run_route(
            working=_route_working(stops=[("Hue", _HUE)]),
            by_name={
                "Hue Spot": [_located("Hue Spot", place_id="p-hue", point=_HUE)],
                "Lang Co Spot": [
                    _located("Lang Co Spot", place_id="p-lc", point=_LANG_CO)
                ],
                "Da Nang Spot": [
                    _located("Da Nang Spot", place_id="p-dn", point=_DA_NANG)
                ],
            },
        )
        assert [c.place.place_name for c in result.candidates] == [
            "Da Nang Spot",
            "Lang Co Spot",
            "Hue Spot",
        ]

    async def test_off_route_candidate_is_dropped(self) -> None:
        """Hoi An lies the other way from a Hue trip. It validated fine — the
        route filter is what removes it."""
        result, _, _ = await _run_route(
            working=_route_working(stops=[("Hue", _HUE)]),
            by_name={
                "Hoi An Spot": [
                    _located("Hoi An Spot", place_id="p-ha", point=_HOI_AN)
                ],
                "Lang Co Spot": [
                    _located("Lang Co Spot", place_id="p-lc", point=_LANG_CO)
                ],
            },
        )
        assert [c.place.place_name for c in result.candidates] == ["Lang Co Spot"]

    async def test_validation_still_costs_one_provider_call_per_name(self) -> None:
        """A route is covered by ONE enclosing disc, so call count is
        unchanged from an ordinary turn — the budget promise of this step."""
        _, _, search = await _run_route(
            working=_route_working(stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)]),
            by_name={
                "A": [_located("A", place_id="p-a", point=_LANG_CO)],
                "B": [_located("B", place_id="p-b", point=_HUE)],
            },
        )
        assert search.find.await_count == 2

    async def test_validation_disc_covers_the_whole_route(self) -> None:
        _, _, search = await _run_route(
            working=_route_working(stops=[("Hue", _HUE)]),
            by_name={"A": [_located("A", place_id="p-a", point=_HUE)]},
        )
        location = search.find.await_args_list[0].args[0].location
        for point in (_DA_NANG, _HUE):
            assert (
                haversine_m(location.lat, location.lng, *point) <= location.radius_m
            )

    async def test_nothing_on_the_route_is_an_honest_empty(self) -> None:
        result, _, _ = await _run_route(
            working=_route_working(stops=[("Hue", _HUE)]),
            by_name={
                "Hoi An Spot": [
                    _located("Hoi An Spot", place_id="p-ha", point=_HOI_AN)
                ]
            },
        )
        assert result.candidates == []
        assert result.empty_reason == "no_match"

    async def test_city_scale_route_spends_nothing(self) -> None:
        """Hanoi → Saigon: the honest stops are cities, which consult cannot
        return yet. No namer call, no provider call, and a distinct reason the
        agent turns into "which stretch?"."""
        namer = _make_namer([CandidateName(name="X", reason="r")])
        factory, search = _make_search_factory(by_name={})
        command = await _run_suggest_places(
            namer=namer,
            places_search_factory=factory,
            state=_state(
                working_location=_route_working(
                    stops=[("Saigon", _SAIGON)], origin=_HANOI, city="Hanoi"
                )
            ),
            tool_call_id="call-long",
            query="road trip",
            categories=None,
            tags=None,
            neighborhood_override=None,
            city_override=None,
            country_override=None,
            limit=5,
            name_count=8,
            concurrency=5,
        )
        message = command.update["messages"][0]
        assert isinstance(message, ToolMessage)
        result = ConsultResult.model_validate_json(str(message.content))
        assert result.empty_reason == "route_too_long"
        namer.generate.assert_not_awaited()
        search.find.assert_not_awaited()


# ---------------------------------------------------------------------------
# Agent-supplied candidate names (ADR-137)
# ---------------------------------------------------------------------------


async def _run_with_names(
    *,
    working: dict[str, Any],
    names: list[str] | None,
    by_name: dict[str, list[PlaceObject]],
    namer_candidates: list[CandidateName] | None = None,
    limit: int = 5,
) -> tuple[ConsultResult, MagicMock, MagicMock]:
    namer = _make_namer(namer_candidates or [])
    factory, search = _make_search_factory(by_name=by_name)
    command = await _run_suggest_places(
        namer=namer,
        places_search_factory=factory,
        state=_state(working_location=working),
        tool_call_id="call-names",
        query="somewhere to stop",
        place_names=names,
        categories=None,
        tags=None,
        neighborhood_override=None,
        city_override=None,
        country_override=None,
        limit=limit,
        name_count=8,
        concurrency=5,
    )
    message = command.update["messages"][0]
    assert isinstance(message, ToolMessage)
    return ConsultResult.model_validate_json(str(message.content)), namer, search


class TestAgentSuppliedNames:
    async def test_agent_names_skip_the_namer_entirely(self) -> None:
        """The orchestrator is the strongest model in the turn — when it knows
        the area, asking a weaker helper to guess is wasted latency and cost."""
        result, namer, _ = await _run_with_names(
            working=_bangkok_working(),
            names=["Wat Pho"],
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]},
        )
        namer.generate.assert_not_awaited()
        assert [c.place.place_name for c in result.candidates] == ["Wat Pho"]

    async def test_agent_names_are_still_validated(self) -> None:
        """A name the agent invents is dropped exactly like one the namer
        invents — this is what keeps "a card must be validated" true."""
        result, _, _ = await _run_with_names(
            working=_bangkok_working(),
            names=["Wat Pho", "Entirely Made Up Place"],
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]},
        )
        assert [c.place.place_name for c in result.candidates] == ["Wat Pho"]

    async def test_agent_named_candidates_carry_no_tool_reason(self) -> None:
        """The agent writes the rationale in its own prose; a tool-layer reason
        would be invented, and an empty string would render as a blank line."""
        result, _, _ = await _run_with_names(
            working=_bangkok_working(),
            names=["Wat Pho"],
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]},
        )
        assert result.candidates[0].reason is None

    async def test_blank_names_are_ignored(self) -> None:
        result, namer, _ = await _run_with_names(
            working=_bangkok_working(),
            names=["   ", ""],
            namer_candidates=[CandidateName(name="Wat Pho", reason="iconic")],
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]},
        )
        # All names were blank, so this is the same as supplying none: the
        # namer runs as the fallback rather than the turn returning empty.
        namer.generate.assert_awaited()
        assert [c.place.place_name for c in result.candidates] == ["Wat Pho"]

    async def test_no_names_leaves_the_namer_path_untouched(self) -> None:
        result, namer, _ = await _run_with_names(
            working=_bangkok_working(),
            names=None,
            namer_candidates=[CandidateName(name="Wat Pho", reason="iconic")],
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]},
        )
        namer.generate.assert_awaited()
        assert result.candidates[0].reason == "iconic"

    async def test_agent_names_on_a_route_are_filtered_and_ordered(self) -> None:
        """The two features compose: the agent names the stops it knows, and
        the route geometry still decides which are on the way and in what
        order."""
        result, namer, _ = await _run_with_names(
            working=_route_working(stops=[("Hue", _HUE)]),
            names=["Hue Spot", "Hoi An Spot", "Da Nang Spot"],
            by_name={
                "Hue Spot": [_located("Hue Spot", place_id="p-h", point=_HUE)],
                "Hoi An Spot": [
                    _located("Hoi An Spot", place_id="p-ha", point=_HOI_AN)
                ],
                "Da Nang Spot": [
                    _located("Da Nang Spot", place_id="p-dn", point=_DA_NANG)
                ],
            },
        )
        namer.generate.assert_not_awaited()
        assert [c.place.place_name for c in result.candidates] == [
            "Da Nang Spot",
            "Hue Spot",
        ]


# ---------------------------------------------------------------------------
# Name round-trip on validation + cross-tool dedupe (ADR-138)
# ---------------------------------------------------------------------------


class TestBestNameMatch:
    def test_exact_name_beats_a_nearer_wrong_one(self) -> None:
        """The Tam Coc bug: a guesthouse sat closer to the search centre than
        the karst boat ride, so nearest-first handed back the guesthouse."""
        hits = [
            _place("Tam Coc Homestay Of Ms Loan", place_id="p-home"),
            _place("Tam Coc", place_id="p-real"),
        ]
        assert _best_name_match("Tam Coc", hits).id == "p-real"

    def test_fewest_extra_words_wins_when_nothing_is_exact(self) -> None:
        """"Marble Mountains" has no exact record — "The Marble Mountains"
        should still beat "Marble Mountains Ticket Booth (Gate A1)"."""
        hits = [
            _place("Marble Mountains Ticket Booth (Gate A1)", place_id="p-booth"),
            _place("The Marble Mountains", place_id="p-real"),
        ]
        assert _best_name_match("Marble Mountains", hits).id == "p-real"

    def test_shorter_name_wins_a_tie_on_extra_words(self) -> None:
        """Both add one word, but "The Marble Mountains" is the mountains and
        "Marble Mountains Elevator" is a lift at them."""
        hits = [
            _place("Marble Mountains Elevator", place_id="p-lift"),
            _place("The Marble Mountains", place_id="p-real"),
        ]
        assert _best_name_match("Marble Mountains", hits).id == "p-real"

    def test_localized_name_still_returns(self) -> None:
        """A preference, not a filter: "Bach Ma National Park" comes back as
        "Vườn Quốc Gia Bạch Mã" and must not be rejected for it."""
        hits = [_place("Vườn Quốc Gia Bạch Mã", place_id="p-bm")]
        assert _best_name_match("Bach Ma National Park", hits).id == "p-bm"

    def test_diacritics_do_not_break_an_exact_match(self) -> None:
        hits = [
            _place("Somewhere Else", place_id="p-x"),
            _place("Hội An", place_id="p-ha"),
        ]
        assert _best_name_match("Hoi An", hits).id == "p-ha"

    def test_no_hits_is_none(self) -> None:
        assert _best_name_match("Anything", []) is None

    def test_nearest_first_order_survives_with_no_name_signal(self) -> None:
        hits = [_place("Alpha", place_id="p-1"), _place("Beta", place_id="p-2")]
        assert _best_name_match("Gamma", hits).id == "p-1"


class TestCrossToolDedupe:
    async def test_place_already_returned_by_find_saved_is_dropped(self) -> None:
        """One place, one card. Before this it shipped twice — once as a save,
        once as a suggestion — under two recommendation_ids."""
        saved = ConsultResult(
            candidates=[
                ConsultCandidate(
                    place=_place("Lap An Lagoon", place_id="p-lap").to_core(),
                    source="saved",
                    rrf_score=0.5,
                )
            ]
        )
        state = _state(working_location=_bangkok_working())
        state["messages"] = [
            ToolMessage(
                content=saved.model_dump_json(),
                tool_call_id="tc-prior",
                name="find_saved",
            )
        ]
        namer = _make_namer(
            [
                CandidateName(name="Lap An Lagoon", reason="on the water"),
                CandidateName(name="Wat Pho", reason="iconic"),
            ]
        )
        factory, _ = _make_search_factory(
            by_name={
                "Lap An Lagoon": [_place("Lap An Lagoon", place_id="p-lap")],
                "Wat Pho": [_place("Wat Pho", place_id="p-wp")],
            }
        )
        command = await _run_suggest_places(
            namer=namer,
            places_search_factory=factory,
            state=state,
            tool_call_id="tc-2",
            query="anything good",
            categories=None,
            tags=None,
            neighborhood_override=None,
            city_override=None,
            country_override=None,
            limit=5,
            name_count=8,
            concurrency=5,
        )
        result = ConsultResult.model_validate_json(
            str(command.update["messages"][0].content)
        )
        assert [c.place.place_name for c in result.candidates] == ["Wat Pho"]

    async def test_unrelated_tool_payloads_are_ignored(self) -> None:
        """`research` returns a different shape on the same message list —
        parsing it must not blow up the turn."""
        state = _state(working_location=_bangkok_working())
        state["messages"] = [
            ToolMessage(
                content='{"entity_name": "Bangkok", "notes": []}',
                tool_call_id="tc-research",
                name="research",
            )
        ]
        namer = _make_namer([CandidateName(name="Wat Pho", reason="iconic")])
        factory, _ = _make_search_factory(
            by_name={"Wat Pho": [_place("Wat Pho", place_id="p-wp")]}
        )
        command = await _run_suggest_places(
            namer=namer,
            places_search_factory=factory,
            state=state,
            tool_call_id="tc-3",
            query="anything good",
            categories=None,
            tags=None,
            neighborhood_override=None,
            city_override=None,
            country_override=None,
            limit=5,
            name_count=8,
            concurrency=5,
        )
        result = ConsultResult.model_validate_json(
            str(command.update["messages"][0].content)
        )
        assert [c.place.place_name for c in result.candidates] == ["Wat Pho"]
