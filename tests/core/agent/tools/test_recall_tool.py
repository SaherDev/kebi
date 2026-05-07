"""Tests for the recall_tool @tool wrapper (feature 028 M5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from kebi.api.schemas.recall import RecallResponse
from kebi.api.schemas.recall import RecallResult as RecallResultSchema
from kebi.core.agent.tools.recall_tool import (
    RecallToolInput,
    build_recall_tool,
)
from kebi.core.places.models import PlaceObject, PlaceType


def _place(pid: str) -> PlaceObject:
    return PlaceObject(
        place_id=pid,
        place_name=pid,
        place_type=PlaceType.food_and_drink,
        subcategory="cafe",
    )


def test_llm_visible_schema_hides_user_id_and_location() -> None:
    schema = RecallToolInput.model_json_schema()
    props = set(schema["properties"].keys())
    assert props == {"query", "filters", "sort_by", "limit"}


@pytest.mark.asyncio
async def test_tool_reads_user_id_and_location_from_state() -> None:
    service = AsyncMock()
    service.run = AsyncMock(
        return_value=RecallResponse(
            results=[RecallResultSchema(place=_place("p1"), match_reason="filter")],
            total_count=1,
        )
    )
    tool = build_recall_tool(service)

    state: dict[str, Any] = {
        "user_id": "u-from-state",
        "location": {"lat": 13.75, "lng": 100.5},
        "reasoning_steps": [],
    }
    result = await tool.coroutine(
        query="coffee",
        filters=None,
        sort_by="relevance",
        limit=10,
        state=state,
        tool_call_id="tc-123",
    )

    call = service.run.await_args
    assert call.kwargs["user_id"] == "u-from-state"
    assert call.kwargs["location"] == (13.75, 100.5)
    assert call.kwargs["query"] == "coffee"
    assert call.kwargs["limit"] == 10
    # Command(update={...}) exposes `update` attribute.
    update = result.update
    assert [p.place_id for p in update["last_recall_results"]] == ["p1"]


@pytest.mark.asyncio
async def test_tool_returns_tool_summary_as_last_user_visible_step() -> None:
    service = AsyncMock()
    service.run = AsyncMock(
        return_value=RecallResponse(
            results=[RecallResultSchema(place=_place("p1"), match_reason="filter")],
            total_count=1,
        )
    )
    tool = build_recall_tool(service)

    state: dict[str, Any] = {
        "user_id": "u",
        "location": None,
        "reasoning_steps": [],
    }
    result = await tool.coroutine(
        query="coffee",
        filters=None,
        sort_by="relevance",
        limit=10,
        state=state,
        tool_call_id="tc-1",
    )

    steps = result.update["reasoning_steps"]
    # Last step must be the user-visible tool.summary.
    last = steps[-1]
    assert last.step == "tool.summary"
    assert last.visibility == "user"
    assert last.tool_name == "recall"
