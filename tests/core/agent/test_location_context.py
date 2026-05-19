"""Tests for _render_location_context — raw GPS coords are surfaced to the
agent when present, with a clear prompt to ask when absent. The server-side
"City, Country" label was removed with the v1 places store (ADR-078).
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import _render_location_context


def _state(location: dict[str, float] | None = None) -> Any:
    return cast(Any, {"location": location})


def test_location_renders_gps_coords() -> None:
    text = _render_location_context(_state(location={"lat": 52.12, "lng": 11.62}))
    assert "lat=52.12" in text
    assert "lng=11.62" in text
    assert "Do NOT ask for their city" in text


def test_no_location_prompts_agent_to_ask() -> None:
    text = _render_location_context(_state(location=None))
    assert "No location provided" in text
