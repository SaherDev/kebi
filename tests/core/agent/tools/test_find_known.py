"""Tests for the `find_known` agent tool (ADR-138)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.tools.find_known_tool import _run_find_known, build_find_known_tool
from kebi.core.knowledge.known_places_service import KnownPlace
from kebi.core.knowledge.research_models import ResearchNote
from kebi.core.places.models import PlaceCore

_CANGGU = {
    "country": "Indonesia",
    "country_code": "id",
    "city": "Bali",
    "neighborhood": "Canggu",
    "lat": -8.65,
    "lng": 115.13,
    "search_radius_m": 3000.0,
}


def _state(
    *, working_location: dict[str, Any] | None = None, local_time: str | None = None
) -> dict[str, Any]:
    return {
        "messages": [],
        "user_id": "user-1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": working_location,
        "location_clarification": None,
        "movement_profile": None,
        "trip_movement": None,
        "user_profile": None,
        "local_time": local_time,
        "reasoning_steps": [],
        "tool_results": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


def _known(name: str, note: str) -> KnownPlace:
    return KnownPlace(
        place=PlaceCore(id=f"id-{name}", place_name=name),
        notes=[ResearchNote(id="n1", text=note, source="community", confidence=0.9)],
        score=7.0,
    )


def _payload(cmd: Command[Any]) -> dict[str, Any]:
    """The untrimmed result from the server-side channel (ADR-139)."""
    return cmd.update["tool_payloads"][-1]["payload"]


def _agent_view(cmd: Command[Any]) -> dict[str, Any]:
    """The lean projection the model actually reads."""
    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    return json.loads(msg.content)


async def _run(service: MagicMock, **kw: Any) -> Command[Any]:
    return await _run_find_known(
        known_places=service,
        notes_service=None,
        state=kw.pop("state", _state(working_location=_CANGGU)),
        tool_call_id="tc-1",
        query=kw.pop("query", "monday night out"),
        tags=kw.pop("tags", None),
        limit=kw.pop("limit", 5),
    )


def _service(result: list[KnownPlace]) -> MagicMock:
    service = MagicMock()
    service.find = AsyncMock(return_value=result)
    return service


async def test_known_places_come_back_as_candidates_with_their_notes() -> None:
    cmd = await _run(_service([_known("Luigi's", "Monday is the big night")]))
    candidate = _payload(cmd)["candidates"][0]
    assert candidate["place"]["place_name"] == "Luigi's"
    assert candidate["source"] == "known"
    assert candidate["notes"][0]["text"] == "Monday is the big night"


async def test_the_model_sees_the_notes_not_the_retrieval_plumbing() -> None:
    """The claims are the point of this tool, so they survive the trim while
    ids and scores do not (ADR-139)."""
    cmd = await _run(_service([_known("Luigi's", "Monday is the big night")]))
    view = _agent_view(cmd)
    candidate = view["candidates"][0]
    # Fact plus coarse origin: the answer must say how kebi knows, so the
    # origin label survives the trim alongside the text.
    assert candidate["kebi_knows"] == [
        {"fact": "Monday is the big night", "from": "community"}
    ]
    assert candidate["name"] == "Luigi's"
    flat = json.dumps(view)
    for leaked in ("rrf_score", "vector_rank", "place_id", "provider_id"):
        assert leaked not in flat


async def test_the_turns_weekday_is_passed_to_retrieval() -> None:
    """The clock is what makes a schedule claim findable without being told."""
    service = _service([])
    await _run(
        service,
        state=_state(working_location=_CANGGU, local_time="2026-08-10T19:30:00+08:00"),
    )
    assert service.find.await_args.kwargs["day"] == "Monday"


async def test_no_clock_means_no_day_asserted() -> None:
    service = _service([])
    await _run(service, state=_state(working_location=_CANGGU))
    assert service.find.await_args.kwargs["day"] is None


async def test_an_unparseable_clock_degrades_instead_of_failing() -> None:
    service = _service([])
    await _run(
        service, state=_state(working_location=_CANGGU, local_time="last tuesday-ish")
    )
    assert service.find.await_args.kwargs["day"] is None


async def test_no_working_location_asks_instead_of_searching() -> None:
    service = _service([])
    payload = _payload(await _run(service, state=_state(working_location=None)))
    assert payload["empty_reason"] == "no_location"
    service.find.assert_not_awaited()


async def test_nothing_known_is_an_honest_empty() -> None:
    payload = _payload(await _run(_service([])))
    assert payload["empty_reason"] == "no_match"
    assert payload["candidates"] == []


async def test_the_call_counts_against_the_turns_tool_budget() -> None:
    cmd = await _run(_service([_known("X", "note")]))
    assert cmd.update["tool_calls_used"] == 1


async def test_reasoning_step_names_what_was_found() -> None:
    cmd = await _run(_service([_known("Luigi's", "Monday is the big night")]))
    step = cmd.update["reasoning_steps"][0]
    assert step.visibility == "user"
    assert "Luigi's" in step.summary


def test_the_tool_constructs_with_its_schema() -> None:
    tool = build_find_known_tool(_service([]))
    assert tool.name == "find_known"
    assert set(tool.args) >= {"query", "tags", "limit"}
