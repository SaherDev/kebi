"""Tests for the consult_tool @tool wrapper (feature 028 M5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from kebi.api.schemas.consult import ConsultResponse, ConsultResult
from kebi.core.agent.tools.consult_tool import (
    ConsultToolInput,
    _consult_summary,
    build_consult_tool,
)
from kebi.core.places.filters import ConsultFilters
from kebi.core.places.models import PlaceObject, PlaceType


def _place(pid: str) -> PlaceObject:
    return PlaceObject(
        place_id=pid,
        place_name=pid,
        place_type=PlaceType.food_and_drink,
    )


def test_llm_visible_schema_hides_saved_places_user_id_location() -> None:
    schema = ConsultToolInput.model_json_schema()
    props = set(schema["properties"].keys())
    assert props == {
        "query",
        "filters",
        "limit",
        "preference_context",
        "place_suggestions",
    }


def test_consult_summary_nothing_matched() -> None:
    resp = ConsultResponse(results=[])
    assert _consult_summary(resp) == "Nothing matched nearby"


def test_consult_summary_mixed() -> None:
    resp = ConsultResponse(
        results=[
            ConsultResult(place=_place("p1"), source="saved"),
            ConsultResult(place=_place("p2"), source="discovered"),
            ConsultResult(place=_place("p3"), source="discovered"),
        ]
    )
    summary = _consult_summary(resp)
    assert "3 options" in summary
    assert "1 saved" in summary and "2 nearby" in summary


def test_consult_summary_discovered_only() -> None:
    resp = ConsultResponse(
        results=[ConsultResult(place=_place("p1"), source="discovered")]
    )
    assert _consult_summary(resp) == "Ranked 1 nearby options"


def test_consult_summary_saved_only() -> None:
    resp = ConsultResponse(results=[ConsultResult(place=_place("p1"), source="saved")])
    assert _consult_summary(resp) == "Ranked 1 from your saves"


@pytest.mark.asyncio
async def test_consult_tool_reads_saved_places_from_state() -> None:
    service = AsyncMock()
    service.consult = AsyncMock(
        return_value=ConsultResponse(
            results=[ConsultResult(place=_place("p1"), source="saved")]
        )
    )
    tool = build_consult_tool(service)

    saved = [_place("saved-from-state")]
    state: dict[str, Any] = {
        "user_id": "u",
        "location": None,
        "last_recall_results": saved,
        "reasoning_steps": [],
    }
    result = await tool.coroutine(
        query="ramen",
        filters=ConsultFilters(),
        limit=3,
        preference_context=None,
        state=state,
        tool_call_id="tc-1",
    )

    call = service.consult.await_args
    # saved_places from state passed through.
    assert call.kwargs["saved_places"] == saved
    # consult tool does NOT write last_recall_results.
    assert "last_recall_results" not in result.update
    assert result.update["reasoning_steps"][-1].step == "tool.summary"
    assert result.update["reasoning_steps"][-1].tool_name == "consult"
