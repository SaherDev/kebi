"""Tests for _render_movement_context — the {movement_context} prompt slot.

The slot renders the search scope the resolve_location node folded onto the
working location (ADR-084), and flags when the request carried no movement
profile so the agent asks rather than asserts a distance.
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import _render_movement_context, _render_system_prompt


def _state(
    working_location: dict[str, Any] | None = None,
    movement_profile: dict[str, Any] | None = None,
) -> Any:
    return cast(
        Any,
        {
            "working_location": working_location,
            "location_clarification": None,
            "movement_profile": movement_profile,
            "taste_profile_summary": "",
            "memory_summary": "",
        },
    )


_AREA_WL = {
    "country": "Thailand",
    "city": "Bangkok",
    "neighborhood": "Thonglor",
    "lat": 13.73,
    "lng": 100.58,
    "effective_mode": "driving",
    "scope_tier": "city",
    "scope_shape": "area",
    "search_radius_m": 18200.0,
    "corridor": None,
}
_PROFILE = {
    "available_modes": ["driving", "walking"],
    "reach": "normal",
}


def test_renders_resolved_area_scope() -> None:
    text = _render_movement_context(
        _state(working_location=_AREA_WL, movement_profile=_PROFILE)
    )
    assert "driving" in text
    assert "city" in text
    assert "18" in text  # ~18.2 km


def test_renders_corridor_scope_with_destination() -> None:
    wl = {
        **_AREA_WL,
        "scope_shape": "corridor",
        "corridor": {
            "stops": [{"name": "Suvarnabhumi Airport", "lat": 13.69, "lng": 100.75}]
        },
    }
    text = _render_movement_context(
        _state(working_location=wl, movement_profile=_PROFILE)
    )
    assert "Suvarnabhumi Airport" in text
    assert "journey" in text


def test_renders_multi_stop_route_in_order() -> None:
    """A chain names every stop, in the order the user said them — the agent
    narrates the journey, so it must see the whole route (ADR-136)."""
    wl = {
        **_AREA_WL,
        "city": "Hanoi",
        "scope_shape": "corridor",
        "corridor": {
            "stops": [
                {"name": "Hue", "lat": 16.46, "lng": 107.59},
                {"name": "Hoi An", "lat": 15.88, "lng": 108.33},
            ]
        },
    }
    text = _render_movement_context(
        _state(working_location=wl, movement_profile=_PROFILE)
    )
    assert "Hanoi → Hue → Hoi An" in text
    assert "ORDERED" in text
    assert "route_too_long" in text
    # The composition rule: saves enrich the journey, they never replace it.
    # Answering a journey question with only the user's saves is worse than
    # what they'd get from asking anyone else.
    assert "find_saved" in text
    assert "place_names" in text


def test_no_profile_flags_fallback_on_resolved_turn() -> None:
    """A resolved turn whose request omitted the profile still renders scope,
    but caveats that a neutral fallback — not the user's profile — was used."""
    text = _render_movement_context(
        _state(working_location=_AREA_WL, movement_profile=None)
    )
    assert "fallback" in text.lower()
    assert "ask" in text.lower()


def test_no_location_and_no_profile_asks_when_load_bearing() -> None:
    text = _render_movement_context(
        _state(working_location=None, movement_profile=None)
    )
    assert "ask" in text.lower()
    assert "get around" in text.lower()


def test_system_prompt_fills_movement_slot_without_keyerror() -> None:
    """_render_system_prompt must format the {movement_context} slot — a
    missing/extra slot would raise KeyError on .format()."""
    static_head, dynamic_tail = _render_system_prompt(
        _state(working_location=_AREA_WL, movement_profile=_PROFILE)
    )
    prompt = f"{static_head}\n\n{dynamic_tail}"
    assert "driving" in prompt
    assert "{movement_context}" not in prompt
    # The per-turn movement slot resolves in the dynamic tail, never the
    # cacheable static head (ADR-100).
    assert "driving" in dynamic_tail
    assert "{" not in static_head
