"""Tests for _render_location_context — proves location_label is surfaced
to the agent when present, with a clear "unknown" fallback when absent.
"""

from __future__ import annotations

from typing import Any, cast

from kebi.core.agent.graph import _render_location_context


def _state(
    location: dict[str, float] | None = None,
    location_label: str | None = None,
) -> Any:
    return cast(
        Any,
        {"location": location, "location_label": location_label},
    )


def test_location_with_label_mentions_city() -> None:
    text = _render_location_context(
        _state(
            location={"lat": 52.12, "lng": 11.62},
            location_label="Magdeburg, Germany",
        )
    )
    assert "lat=52.12" in text
    assert "Magdeburg, Germany" in text
    assert "unknown" not in text


def test_location_without_label_falls_back_to_unknown_clause() -> None:
    text = _render_location_context(
        _state(location={"lat": 52.12, "lng": 11.62}, location_label=None)
    )
    assert "lat=52.12" in text
    assert "city is unknown" in text


def test_no_location_prompts_agent_to_ask() -> None:
    text = _render_location_context(_state(location=None, location_label=None))
    assert "No location provided" in text
