"""Tests for _render_location_context — the {location_context} prompt slot.

The slot now renders the resolved working location, a clarification reason, or
a generic no-location fallback (location-context feature). The old flat-GPS
rendering was replaced when the start-of-turn resolution flow landed.
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import _render_location_context


def _state(
    working_location: dict[str, Any] | None = None,
    location_clarification: str | None = None,
) -> Any:
    return cast(
        Any,
        {
            "working_location": working_location,
            "location_clarification": location_clarification,
        },
    )


def test_renders_resolved_working_location() -> None:
    text = _render_location_context(
        _state(
            working_location={
                "country": "Thailand",
                "city": "Bangkok",
                "neighborhood": "Thonglor",
                "lat": 13.73,
                "lng": 100.58,
            }
        )
    )
    assert "Thonglor, Bangkok, Thailand" in text
    assert "lat=13.73" in text
    assert "lng=100.58" in text


def test_renders_working_location_without_neighborhood() -> None:
    """A user_actual location may have no neighborhood — render without it."""
    text = _render_location_context(
        _state(
            working_location={
                "country": "Thailand",
                "city": "Bangkok",
                "neighborhood": None,
                "lat": 13.73,
                "lng": 100.58,
            }
        )
    )
    assert "Bangkok, Thailand" in text
    assert "None" not in text


def test_renders_clarification_reason() -> None:
    text = _render_location_context(
        _state(location_clarification="Cambridge in the UK or Massachusetts?")
    )
    assert "Cambridge in the UK or Massachusetts?" in text
    assert "clarify" in text.lower()


def test_no_location_prompts_agent_to_ask() -> None:
    text = _render_location_context(_state())
    assert "No location resolved" in text
