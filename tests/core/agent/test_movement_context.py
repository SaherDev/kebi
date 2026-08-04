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


# --- Per-leg scale (ADR-138) -----------------------------------------------


def _chain_wl(stops: list[dict[str, Any]], city: str = "Hanoi") -> dict[str, Any]:
    return {
        **_AREA_WL,
        "country": "Vietnam",
        "city": city,
        "lat": 21.0278,
        "lng": 105.8342,
        "scope_shape": "corridor",
        "corridor": {"stops": stops},
    }


_HUE = {"name": "Hue", "lat": 16.3546659, "lng": 107.4795173}
_HOI_AN = {"name": "Hoi An", "lat": 15.8685, "lng": 108.3267}


def test_mixed_scale_chain_flags_the_undrivable_leg() -> None:
    """The whole point: we already know Hanoi→Hue is 548 km and skip sampling
    it, but until now never told the agent — so it narrated a road trip
    through a leg everyone flies."""
    text = _render_movement_context(
        _state(working_location=_chain_wl([_HUE, _HOI_AN]), movement_profile=_PROFILE)
    )
    assert "Hanoi → Hue" in text
    assert "548 km" in text
    assert "TOO FAR TO DRIVE WITH STOPS" in text
    # The short leg is still a drive.
    assert "Hue → Hoi An" in text
    assert "105 km" in text
    # And it must say what to do instead of narrating a drive.
    assert "overnight train" in text
    assert "destinations in their own right" in text


def test_all_drivable_chain_gets_no_transport_advice() -> None:
    """A short chain is just a drive — don't clutter it with flight talk."""
    da_nang = _chain_wl([_HUE], city="Da Nang")
    da_nang["lat"], da_nang["lng"] = 16.0544, 108.2022
    text = _render_movement_context(
        _state(working_location=da_nang, movement_profile=_PROFILE)
    )
    assert "Da Nang → Hue" in text
    assert "TOO FAR TO DRIVE WITH STOPS" not in text
    assert "overnight train" not in text


def test_unparseable_corridor_state_degrades_quietly() -> None:
    """`working_location` is raw checkpointed state — a shape that no longer
    validates must lose the leg block, not the turn."""
    broken = _chain_wl([{"name": "Hue"}])  # no coordinates
    text = _render_movement_context(
        _state(working_location=broken, movement_profile=_PROFILE)
    )
    assert "TOO FAR TO DRIVE" not in text
    assert "Search scope for this turn" in text
