"""Tests for the `web_search` agent tool (ADR-145).

Exercises `_run_web_search` directly with a stubbed `WebKnowledgeService`.
Asserts the Command envelope, the lean projection the model reads, the
server-side payload the harvest reads, and the location plumbing.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools.web_search_tool import (
    _run_web_search,
    build_web_search_tool,
)
from kebi.core.web.models import WebFinding, WebSearchResult


def _working() -> dict[str, Any]:
    return WorkingLocation(
        country="Indonesia",
        country_code="id",
        city="Badung",
        neighborhood="Canggu",
        lat=-8.65,
        lng=115.13,
        search_radius_m=5000.0,
    ).model_dump()


def _state(**kw: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [],
        "user_id": "user-1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": _working(),
        "location_clarification": None,
        "movement_profile": None,
        "trip_movement": None,
        "user_profile": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }
    state.update(kw)
    return state


def _result(**kw: Any) -> WebSearchResult:
    base: dict[str, Any] = {
        "query": "full moon party dates Canggu",
        "findings": [
            WebFinding(
                text="The next full moon party is on the 12th.",
                source="partycal.example",
                age="2 days ago",
                url="https://partycal.example/p",
            )
        ],
        "country_code": "id",
        "city": "Badung",
        "neighborhood": "Canggu",
    }
    base.update(kw)
    return WebSearchResult(**base)


def _service(result: WebSearchResult) -> MagicMock:
    service = MagicMock()
    service.search = AsyncMock(return_value=result)
    return service


async def _run(service: MagicMock, **kw: Any) -> Command[Any]:
    defaults: dict[str, Any] = {
        "web_knowledge": service,
        "state": _state(),
        "tool_call_id": "call-1",
        "query": "full moon party dates",
        "freshness": None,
        "limit": None,
    }
    defaults.update(kw)
    return await _run_web_search(**defaults)


def _tool_message(command: Command[Any]) -> ToolMessage:
    return command.update["messages"][0]  # type: ignore[index]


# --- what the model reads --------------------------------------------------


async def test_the_model_reads_text_source_and_age() -> None:
    command = await _run(_service(_result()))
    view = json.loads(_tool_message(command).content)
    assert view["findings"] == [
        {
            "text": "The next full moon party is on the 12th.",
            "source": "partycal.example",
            "published": "2 days ago",
        }
    ]


async def test_the_model_never_sees_a_url() -> None:
    """It cannot follow one, the chat contract has nowhere to render one
    (ADR-136), and it is 15-30 tokens of pure cost per finding."""
    command = await _run(_service(_result()))
    assert "http" not in _tool_message(command).content


async def test_the_empty_reason_survives_to_the_model() -> None:
    command = await _run(_service(_result(findings=[], empty_reason="no_results")))
    view = json.loads(_tool_message(command).content)
    assert view["empty_reason"] == "no_results"
    assert "findings" not in view


# --- what the server keeps -------------------------------------------------


async def test_the_full_result_rides_the_server_side_channel() -> None:
    """The harvest needs the URL and the area; the model does not."""
    command = await _run(_service(_result()))
    (payload,) = command.update["tool_payloads"]  # type: ignore[index]
    assert payload["tool"] == "web_search"
    assert payload["payload"]["findings"][0]["url"] == "https://partycal.example/p"
    assert payload["payload"]["country_code"] == "id"


# --- plumbing --------------------------------------------------------------


async def test_the_working_location_is_the_search_scope() -> None:
    """No area args on this tool, so the search cannot drift from the
    conversation."""
    service = _service(_result())
    await _run(service)
    assert service.search.await_args.kwargs["working"].neighborhood == "Canggu"


async def test_a_turn_without_a_location_still_searches() -> None:
    """A world question ("when is the world cup final") has no location and
    must not be blocked by one."""
    service = _service(_result())
    await _run(service, state=_state(working_location=None))
    assert service.search.await_args.kwargs["working"] is None


async def test_freshness_reaches_the_service() -> None:
    service = _service(_result())
    await _run(service, freshness="week")
    assert service.search.await_args.kwargs["freshness"] == "week"


async def test_the_call_is_counted_against_the_turn_budget() -> None:
    command = await _run(_service(_result()), state=_state(tool_calls_used=2))
    assert command.update["tool_calls_used"] == 3  # type: ignore[index]


# --- the reasoning row -----------------------------------------------------


async def test_a_successful_search_reports_what_it_read() -> None:
    command = await _run(_service(_result()))
    (step,) = command.update["reasoning_steps"]  # type: ignore[index]
    assert step.summary == "read 1 source — partycal.example"
    assert step.visibility == "user"


async def test_a_missing_backend_reads_differently_than_a_dry_index() -> None:
    """The user-visible difference between "I couldn't check" and "I checked
    and found nothing"."""
    unreachable = await _run(_service(_result(findings=[], empty_reason="no_provider")))
    dry = await _run(_service(_result(findings=[], empty_reason="no_results")))
    assert unreachable.update["reasoning_steps"][0].summary == (  # type: ignore[index]
        "couldn't reach the web just now"
    )
    assert dry.update["reasoning_steps"][0].summary == (  # type: ignore[index]
        "nothing current came back"
    )


# --- the schema the model is shown -----------------------------------------


def test_the_tool_exposes_only_query_freshness_and_limit() -> None:
    tool = build_web_search_tool(_service(_result()))
    assert set(tool.tool_call_schema.model_fields) == {
        "query",
        "freshness",
        "limit",
    }
