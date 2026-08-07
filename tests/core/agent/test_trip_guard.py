"""Trip guard (ADR-150) — a trip answer may not finalize past an empty stop.

Prompt instruction alone proved partial: on an empty-library trip the
orchestrator repeatedly wrote city guides from memory. The guard makes the
rule structural — routing, not persuasion — while staying bounded: one
nudge per turn, never past the tool budget.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import (
    _TRIP_GUARD_MARKER,
    NODE_TRIP_GUARD,
    should_continue,
    trip_guard_node,
)
from kebi.core.agent.location import ItineraryAnchor, WorkingLocation


def _itinerary_wl() -> dict[str, Any]:
    return WorkingLocation(
        country="Japan",
        country_code="jp",
        city="Kyoto",
        lat=35.01,
        lng=135.77,
        search_radius_m=7000.0,
        scope_shape="itinerary",
        scope_tier="city",
        itinerary=[
            ItineraryAnchor(name="Kyoto", lat=35.01, lng=135.77),
            ItineraryAnchor(name="Osaka", lat=34.69, lng=135.50),
            ItineraryAnchor(name="Tokyo", lat=35.68, lng=139.69),
        ],
    ).model_dump()


def _saved_payload(*segments: str) -> dict[str, Any]:
    return {
        "tool": "find_saved",
        "payload": {
            "candidates": [
                {"place": {"place_name": f"P{i}"}, "segment": seg}
                for i, seg in enumerate(segments)
            ]
        },
    }


def _state(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "messages": [
            HumanMessage(content="kyoto then osaka then tokyo, what to stop at?"),
            AIMessage(content="here is a plan from memory"),
        ],
        "working_location": _itinerary_wl(),
        "tool_payloads": [_saved_payload("Osaka", "Tokyo")],
        "tool_calls_used": 1,
        "steps_taken": 2,
        "error_count": 0,
    }
    base.update(kw)
    return base


def test_a_final_trip_answer_with_an_empty_stop_is_intercepted() -> None:
    assert should_continue(_state()) == NODE_TRIP_GUARD  # type: ignore[arg-type]


def test_the_nudge_names_only_the_uncovered_stops() -> None:
    update = trip_guard_node(_state())  # type: ignore[arg-type]
    text = update["messages"][0].content
    assert text.startswith(_TRIP_GUARD_MARKER)
    assert "Kyoto" in text
    assert "Osaka" not in text and "Tokyo" not in text


def test_every_stop_covered_means_no_guard() -> None:
    state = _state(tool_payloads=[_saved_payload("Kyoto", "Osaka", "Tokyo")])
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_a_suggest_places_round_clears_the_guard() -> None:
    state = _state(
        tool_payloads=[
            _saved_payload("Osaka", "Tokyo"),
            {"tool": "suggest_places", "payload": {"candidates": []}},
        ]
    )
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_the_guard_fires_once_per_turn() -> None:
    state = _state()
    nudge = trip_guard_node(state)["messages"][0]  # type: ignore[arg-type]
    state["messages"] = [
        *state["messages"],
        nudge,
        AIMessage(content="still answering from memory"),
    ]
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_no_budget_means_no_guard() -> None:
    # Room for one more call is required: nudging with an empty budget would
    # send the model into the cap-hit fallback instead of a better answer.
    # One remaining slot IS enough — the nudge asks for exactly one call.
    from kebi.core.config import get_config

    cap = get_config().agent.max_tool_calls
    assert should_continue(_state(tool_calls_used=cap)) == "fallback"  # type: ignore[arg-type]
    assert should_continue(_state(tool_calls_used=cap - 1)) == NODE_TRIP_GUARD  # type: ignore[arg-type]


def test_a_non_itinerary_turn_never_guards() -> None:
    wl = _itinerary_wl() | {"scope_shape": "area", "itinerary": None}
    state = _state(working_location=wl, tool_payloads=[])
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_an_agent_tool_call_still_routes_to_tools() -> None:
    state = _state()
    state["messages"] = [
        *state["messages"][:-1],
        AIMessage(
            content="",
            tool_calls=[{"name": "find_known", "args": {}, "id": "tc1"}],
        ),
    ]
    assert should_continue(state) == "tools"  # type: ignore[arg-type]
