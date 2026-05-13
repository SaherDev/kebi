"""Tests for NodeInterrupt on needs_review saves (feature 028 M8)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.errors import NodeInterrupt

from kebi.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
from kebi.core.agent.tools.save_tool import build_save_tool
from kebi.core.places_v2 import PlaceCategory, PlaceObject


def _place_object(name: str = "Fuji Ramen") -> PlaceObject:
    return PlaceObject(
        id="p1",
        provider_id="google:p1",
        place_name=name,
        categories=[PlaceCategory.restaurant],
    )


def _needs_review_response(place_name: str = "Fuji Ramen") -> ExtractPlaceResponse:
    return ExtractPlaceResponse(
        status="completed",
        results=[
            ExtractPlaceItem(
                place=_place_object(place_name),
                confidence=0.4,
                status="needs_review",
            )
        ],
        raw_input="some url",
    )


def _get_interrupt_payload(exc: NodeInterrupt) -> dict[str, Any]:
    """Extract the payload dict from a LangGraph NodeInterrupt.

    LangGraph wraps the NodeInterrupt as:
      exc.args[0] == [Interrupt(value=<payload>, ...)]
    """
    interrupts = exc.args[0] if exc.args else []
    if interrupts and hasattr(interrupts[0], "value"):
        return interrupts[0].value  # type: ignore[no-any-return]
    return {}


async def test_node_interrupt_raised_on_needs_review() -> None:
    """save_tool raises NodeInterrupt when any result has needs_review status."""
    service = AsyncMock()
    service.run = AsyncMock(return_value=_needs_review_response("Fuji Ramen"))
    tool = build_save_tool(service)

    state: dict[str, Any] = {"user_id": "u1", "reasoning_steps": []}

    with pytest.raises(NodeInterrupt) as exc_info:
        await tool.coroutine(raw_input="some url", state=state, tool_call_id="tc-1")

    interrupt_val = _get_interrupt_payload(exc_info.value)
    assert interrupt_val["type"] == "save_needs_review"
    assert len(interrupt_val["candidates"]) == 1


async def test_node_interrupt_contains_candidate_place_name() -> None:
    """NodeInterrupt payload includes the place name in candidates."""
    service = AsyncMock()
    service.run = AsyncMock(return_value=_needs_review_response("Ichiran Ramen"))
    tool = build_save_tool(service)

    state: dict[str, Any] = {"user_id": "u1", "reasoning_steps": []}

    with pytest.raises(NodeInterrupt) as exc_info:
        await tool.coroutine(raw_input="ichiran", state=state, tool_call_id="tc-2")

    interrupt_val = _get_interrupt_payload(exc_info.value)
    candidates = interrupt_val["candidates"]
    assert candidates[0]["place"]["place_name"] == "Ichiran Ramen"


async def test_no_interrupt_on_saved_status() -> None:
    """save_tool does NOT raise NodeInterrupt when all results are saved."""
    saved_response = ExtractPlaceResponse(
        status="completed",
        results=[
            ExtractPlaceItem(
                place=_place_object("Safe Place"),
                confidence=0.95,
                status="saved",
            )
        ],
        raw_input="some url",
    )
    service = AsyncMock()
    service.run = AsyncMock(return_value=saved_response)
    tool = build_save_tool(service)

    state: dict[str, Any] = {"user_id": "u1", "reasoning_steps": []}

    result = await tool.coroutine(
        raw_input="safe url", state=state, tool_call_id="tc-3"
    )
    # Should return a Command (not raise)
    assert result.update["reasoning_steps"][-1].step == "tool.summary"


async def test_node_interrupt_payload_has_request_id() -> None:
    """NodeInterrupt payload carries the request_id from the ExtractPlaceResponse."""
    response = _needs_review_response()
    service = AsyncMock()
    service.run = AsyncMock(return_value=response)
    tool = build_save_tool(service)

    state: dict[str, Any] = {"user_id": "u1", "reasoning_steps": []}

    with pytest.raises(NodeInterrupt) as exc_info:
        await tool.coroutine(raw_input="url", state=state, tool_call_id="tc-4")

    interrupt_val = _get_interrupt_payload(exc_info.value)
    assert "request_id" in interrupt_val
