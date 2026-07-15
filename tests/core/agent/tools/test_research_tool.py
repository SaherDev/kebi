"""Tests for the `research` agent tool.

Exercises `_run_research` directly with a stubbed `ResearchService`.
Asserts the Command envelope (ToolMessage + reasoning_steps +
tool_calls_used), the single user-visible outcome row per outcome kind,
the working-location + user_id plumbing into the service, the tool's own
arg schema, and the research-shaped degraded payload from with_timeout.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools.research_tool import _run_research, build_research_tool
from kebi.core.knowledge.research_models import ResearchNote, ResearchResult


def _da_nang_working() -> dict[str, Any]:
    return WorkingLocation(
        country="Vietnam",
        city="Da Nang",
        country_code="vn",
        lat=16.05,
        lng=108.2,
        search_radius_m=5000.0,
    ).model_dump()


def _state(
    *,
    working_location: dict[str, Any] | None = None,
    user_id: str = "user-1",
) -> dict[str, Any]:
    return {
        "messages": [],
        "user_id": user_id,
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": working_location,
        "location_clarification": None,
        "movement_profile": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


def _note(text: str = "BIDV charges no fee") -> ResearchNote:
    return ResearchNote(
        id="c-1", text=text, tags=["no_fee_atm"], source="community", confidence=0.8
    )


def _service(result: ResearchResult) -> AsyncMock:
    service = MagicMock()
    service.research = AsyncMock(return_value=result)
    return service


async def _run(service: AsyncMock, **kwargs: Any) -> Command[Any]:
    defaults: dict[str, Any] = {
        "research_service": service,
        "state": _state(working_location=_da_nang_working()),
        "tool_call_id": "tc-1",
        "query": "atm fees",
        "tags": ["no_fee_atm"],
        "neighborhood": None,
        "city": "Da Nang",
        "country": "Vietnam",
        "limit": None,
    }
    defaults.update(kwargs)
    return await _run_research(**defaults)


async def test_happy_path_command_shape() -> None:
    result = ResearchResult(
        entity_name="Da Nang", entity_key="vn/da-nang", notes=[_note()]
    )
    service = _service(result)

    cmd = await _run(service)

    assert isinstance(cmd, Command)
    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert msg.name == "research"
    assert msg.tool_call_id == "tc-1"
    payload = ResearchResult.model_validate_json(msg.content)
    assert payload.notes[0].text == "BIDV charges no fee"
    assert cmd.update["tool_calls_used"] == 1

    steps = cmd.update["reasoning_steps"]
    user_steps = [s for s in steps if s.visibility == "user"]
    assert len(user_steps) == 1
    assert user_steps[0].step == "research.summary"
    assert user_steps[0].title == "dug into the local intel"
    assert "Da Nang" in user_steps[0].summary


async def test_service_receives_args_working_location_and_user_id() -> None:
    service = _service(ResearchResult(entity_name="Da Nang", notes=[_note()]))

    await _run(service, state=_state(working_location=_da_nang_working()))

    call = service.research.await_args
    assert call.kwargs["query"] == "atm fees"
    assert call.kwargs["tags"] == ["no_fee_atm"]
    assert call.kwargs["city"] == "Da Nang"
    assert call.kwargs["country"] == "Vietnam"
    assert call.kwargs["user_id"] == "user-1"
    assert isinstance(call.kwargs["working_location"], WorkingLocation)
    assert call.kwargs["working_location"].country_code == "vn"


async def test_missing_working_location_passes_none() -> None:
    service = _service(
        ResearchResult(empty_reason="ambiguous", clarification="which city?")
    )

    await _run(service, state=_state(working_location=None))

    assert service.research.await_args.kwargs["working_location"] is None


async def test_empty_reasons_map_to_outcome_step_kinds() -> None:
    cases = {
        "unresolved": ResearchResult(
            empty_reason="unresolved", clarification="couldn't verify"
        ),
        "ambiguous": ResearchResult(
            empty_reason="ambiguous", clarification="which country?"
        ),
        "no_claims": ResearchResult(
            entity_name="Da Nang", empty_reason="no_claims", clarification="nothing yet"
        ),
        "no_topic_match": ResearchResult(
            entity_name="Da Nang",
            empty_reason="no_topic_match",
            clarification="not this angle",
        ),
    }
    for kind, result in cases.items():
        cmd = await _run(_service(result))
        payload = ResearchResult.model_validate_json(cmd.update["messages"][0].content)
        assert payload.empty_reason == kind
        user_steps = [
            s for s in cmd.update["reasoning_steps"] if s.visibility == "user"
        ]
        assert user_steps[0].step == f"research.{kind}"


def test_tool_factory_schema_is_research_specific() -> None:
    tool = build_research_tool(MagicMock())
    assert tool.name == "research"
    schema_fields = set(tool.tool_call_schema.model_fields.keys())
    # No `categories` — that's a place-search concept.
    assert schema_fields == {
        "query",
        "tags",
        "neighborhood",
        "city",
        "country",
        "limit",
    }


async def test_degraded_payload_is_research_shaped() -> None:
    """A timeout/crash empties `notes`, not the place tools' `candidates`."""
    from kebi.core.agent.tools._with_timeout import with_timeout

    async def _boom() -> Command[Any]:
        raise RuntimeError("simulated failure")

    cmd = await with_timeout(
        tool_name="research",
        tool_call_id="tc-err",
        seconds=5,
        state=_state(working_location=_da_nang_working()),
        coro=_boom(),
    )
    payload = json.loads(cmd.update["messages"][0].content)
    assert payload["notes"] == []
    assert "candidates" not in payload
    assert payload["empty_reason"] == "error"
    assert cmd.update["reasoning_steps"][-1].step == "research.failure"
    assert cmd.update["reasoning_steps"][-1].title == "dug into the local intel"
