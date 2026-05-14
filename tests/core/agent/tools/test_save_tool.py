"""Tests for the save_tool @tool wrapper.

Spec 030 Phase 6: ExtractPlaceItem.status is gone, so the save tool
no longer branches on per-item status. The summary is just "Saved
{name}" for a single result or "Saved N places: …" for multiple.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from kebi.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
from kebi.core.agent.tools.save_tool import (
    SaveToolInput,
    _save_summary,
    build_save_tool,
)
from kebi.core.places_v2 import PlaceCategory, PlaceObject


def _place_object(name: str = "Fuji Ramen", place_id: str = "p1") -> PlaceObject:
    return PlaceObject(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
    )


def test_llm_visible_schema_is_single_raw_input_field() -> None:
    schema = SaveToolInput.model_json_schema()
    assert set(schema["properties"].keys()) == {"raw_input"}


def test_save_summary_failed() -> None:
    resp = ExtractPlaceResponse(
        status="failed",
        results=[],
        raw_input="x",
        failure_reason="no_candidates",
        failure_message="nothing extractable",
    )
    assert _save_summary(resp) == "Couldn't extract a place from that"


def test_save_summary_single_result() -> None:
    resp = ExtractPlaceResponse(
        status="completed",
        results=[ExtractPlaceItem(place=_place_object(), confidence=0.9)],
        raw_input="x",
    )
    assert _save_summary(resp) == "Saved Fuji Ramen to your places"


def test_save_summary_multiple_results() -> None:
    resp = ExtractPlaceResponse(
        status="completed",
        results=[
            ExtractPlaceItem(place=_place_object("Fuji", "p1"), confidence=0.9),
            ExtractPlaceItem(place=_place_object("Joe", "p2"), confidence=0.9),
        ],
        raw_input="x",
    )
    summary = _save_summary(resp)
    assert "Saved 2 places" in summary
    assert "Fuji" in summary
    assert "Joe" in summary


@pytest.mark.asyncio
async def test_save_tool_does_not_write_last_recall_results() -> None:
    service = AsyncMock()
    service.run = AsyncMock(
        return_value=ExtractPlaceResponse(
            status="completed",
            results=[ExtractPlaceItem(place=_place_object(), confidence=0.9)],
            raw_input="x",
        )
    )
    tool = build_save_tool(service)

    state: dict[str, Any] = {"user_id": "u", "reasoning_steps": []}
    result = await tool.coroutine(
        raw_input="Fuji Ramen", state=state, tool_call_id="tc-1"
    )

    assert "last_recall_results" not in result.update
    # user-visible summary is the last step.
    assert result.update["reasoning_steps"][-1].step == "tool.summary"
    assert result.update["reasoning_steps"][-1].tool_name == "save"
