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


def test_the_guard_injects_one_suggest_places_call_for_the_next_stop() -> None:
    update = trip_guard_node(_state())  # type: ignore[arg-type]
    msg = update["messages"][0]
    assert msg.content.startswith(f"{_TRIP_GUARD_MARKER} target: Kyoto")
    # The guard does not ask the model to call the tool — it calls it.
    # Exactly ONE call: parallel injected Commands would collide on the
    # plain tool_payloads channel.
    assert [c["name"] for c in msg.tool_calls] == ["suggest_places"]
    args = msg.tool_calls[0]["args"]
    assert args["city"] == "Kyoto"
    # No names: the namer fallback produces the picks, provider-verified.
    assert "names" not in args


def test_the_injected_query_leads_with_the_users_taste() -> None:
    state = _state(taste_values=["specialty_coffee", "scenic_view"])
    update = trip_guard_node(state)  # type: ignore[arg-type]
    query = update["messages"][0].tool_calls[0]["args"]["query"]
    # Taste leads (human-readable, underscores dropped) — trailing it
    # returned the tourist canon with taste as an afterthought.
    assert query.startswith("the best specialty coffee, scenic view spots")


def test_every_stop_covered_means_no_guard() -> None:
    state = _state(tool_payloads=[_saved_payload("Kyoto", "Osaka", "Tokyo")])
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_a_stop_covered_by_suggested_candidates_stops_firing() -> None:
    state = _state(
        tool_payloads=[
            _saved_payload("Osaka", "Tokyo"),
            {
                "tool": "suggest_places",
                "payload": {
                    "candidates": [{"place": {"place_name": "W"}, "segment": "Kyoto"}]
                },
            },
        ]
    )
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_a_targeted_stop_is_never_targeted_twice() -> None:
    # Verification came back empty: the stop stays uncovered, but the
    # marker blocks a retry — one round per stop, then the answer stands.
    state = _state()
    nudge = trip_guard_node(state)["messages"][0]  # type: ignore[arg-type]
    state["messages"] = [
        *state["messages"],
        nudge,
        AIMessage(content="still answering, kyoto came back empty"),
    ]
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_each_uncovered_stop_gets_its_own_round() -> None:
    # Two empty stops: after Kyoto's round, the guard targets Osaka next.
    state = _state(tool_payloads=[_saved_payload("Tokyo")])
    first = trip_guard_node(state)["messages"][0]  # type: ignore[arg-type]
    assert first.tool_calls[0]["args"]["city"] == "Kyoto"
    state["messages"] = [
        *state["messages"],
        first,
        AIMessage(content="second draft"),
    ]
    assert should_continue(state) == NODE_TRIP_GUARD  # type: ignore[arg-type]
    second = trip_guard_node(state)["messages"][0]  # type: ignore[arg-type]
    assert second.tool_calls[0]["args"]["city"] == "Osaka"


def test_no_budget_means_no_guard() -> None:
    # Room for one more call is required: intervening with an empty budget
    # would send the model into the cap-hit fallback instead of a better
    # answer. One remaining slot IS enough — the guard injects exactly one
    # call per uncovered stop. Trip turns run on the itinerary budget.
    from kebi.core.config import get_config

    cap = get_config().agent.itinerary.max_tool_calls
    assert should_continue(_state(tool_calls_used=cap)) == "fallback"  # type: ignore[arg-type]
    assert should_continue(_state(tool_calls_used=cap - 1)) == NODE_TRIP_GUARD  # type: ignore[arg-type]


def test_a_trip_turn_runs_on_the_itinerary_budget() -> None:
    # The flat cap collapsed real trip turns into the cap-hit fallback
    # before the guard could ever run.
    from kebi.core.config import get_config

    flat = get_config().agent.max_tool_calls
    assert should_continue(_state(tool_calls_used=flat)) == NODE_TRIP_GUARD  # type: ignore[arg-type]


def test_a_dropped_save_triggers_the_polish_pass() -> None:
    """All stops covered, but the draft never names a save the user owns —
    the deterministic completeness check sends it back once, tool-free."""
    payload = {
        "tool": "find_saved",
        "payload": {
            "candidates": [
                {
                    "place": {"place_name": "Mount Fuji"},
                    "user_data": {"user_place_id": "up1"},
                    "segment": "Kyoto",
                },
                {
                    "place": {"place_name": "Shibuya Sky"},
                    "user_data": {"user_place_id": "up2"},
                    "segment": "Osaka",
                },
                {"place": {"place_name": "Cover"}, "segment": "Tokyo"},
            ]
        },
    }
    state = _state(
        tool_payloads=[payload],
        messages=[
            HumanMessage(content="trip?"),
            AIMessage(content="go see Shibuya Sky at sunset, skip the rest"),
        ],
    )
    assert should_continue(state) == NODE_TRIP_GUARD  # type: ignore[arg-type]
    update = trip_guard_node(state)  # type: ignore[arg-type]
    msg = update["messages"][0]
    assert msg.content.startswith(f"{_TRIP_GUARD_MARKER} target: polish")
    assert "Mount Fuji" in msg.content
    assert "Shibuya Sky" not in msg.content.split("dropped")[1].split(".")[0]
    # Text-only, and user-side: an assistant message with no tool calls at
    # the conversation's end reads as prefill to the Anthropic API (400).
    assert isinstance(msg, HumanMessage)
    assert not getattr(msg, "tool_calls", None)

    # The polish pass runs once: after its marker, the answer stands.
    state["messages"] = [
        *state["messages"],
        msg,
        AIMessage(content="rewritten, still no fuji somehow"),
    ]
    assert should_continue(state) == "end"  # type: ignore[arg-type]


def test_a_draft_naming_every_save_needs_no_polish() -> None:
    payload = {
        "tool": "find_saved",
        "payload": {
            "candidates": [
                {
                    "place": {
                        "place_name": "Kalà Kalà Beach Club",
                        "place_name_aliases": [
                            {"value": "Kala Kala", "source": "manual"}
                        ],
                    },
                    "user_data": {"user_place_id": "up1"},
                    "segment": "Kyoto",
                },
                {"place": {"place_name": "C"}, "segment": "Osaka"},
                {"place": {"place_name": "D"}, "segment": "Tokyo"},
            ]
        },
    }
    # Mentioned via its alias spelling — that counts as named.
    state = _state(
        tool_payloads=[payload],
        messages=[
            HumanMessage(content="trip?"),
            AIMessage(content="kala kala at sunset is the move"),
        ],
    )
    assert should_continue(state) == "end"  # type: ignore[arg-type]


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
