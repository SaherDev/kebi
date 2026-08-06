"""The web-search flywheel: findings become claims after the turn (ADR-145).

The dispatch half — that a turn which searched queues a harvest, and one that
did not does not. Runs in the `finally` alongside TurnCompleted, so the user
never waits on it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest
from kebi.core.chat.service import ChatService, web_search_results


def _web_payload(findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tool": "web_search",
        "tool_call_id": "call-1",
        "payload": {
            "query": "full moon party dates",
            "findings": findings,
            "country_code": "id",
            "city": "Badung",
            "neighborhood": "Canggu",
        },
    }


_FINDING = {"text": "The next one is on the 12th.", "source": "cal.example"}


def _graph(tool_results: list[dict[str, Any]]) -> MagicMock:
    def _factory(*_a: Any, **_kw: Any) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            yield {
                "messages": [],
                "reasoning_steps": [],
                "working_location": None,
                "tool_results": tool_results,
            }
            yield {
                "messages": [AIMessage(content="the next one is on the 12th")],
                "reasoning_steps": [],
                "working_location": None,
                "tool_results": [],
                "tool_calls_used": 1,
            }

        return _gen()

    graph = MagicMock()
    graph.astream = MagicMock(side_effect=_factory)
    return graph


def _service(graph: MagicMock, *, harvest_enabled: bool = True) -> tuple[Any, Any]:
    from kebi.core.config import get_config

    config = get_config().model_copy(deep=True)
    config.agent.web_search.harvest_enabled = harvest_enabled
    taste_service = AsyncMock()
    taste_service.get_taste_profile = AsyncMock(return_value=None)
    memory_service = AsyncMock()
    memory_service.load_memories = AsyncMock(return_value=[])
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    service = ChatService(
        event_dispatcher=dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=config,
        agent_graph=graph,
    )
    return service, dispatcher


async def _run(
    tool_results: list[dict[str, Any]], *, harvest_enabled: bool = True
) -> list[Any]:
    service, dispatcher = _service(
        _graph(tool_results), harvest_enabled=harvest_enabled
    )
    await service.run(ChatRequest(message="when is the next one"), user_id="u-1")
    return [c.args[0] for c in dispatcher.dispatch.await_args_list]


def _harvests(events: list[Any]) -> list[Any]:
    return [e for e in events if e.event_type == "web_findings_harvest_requested"]


# --- the selector ----------------------------------------------------------


def test_only_web_search_payloads_are_selected() -> None:
    results = [
        {"tool": "find_saved", "payload": {"candidates": []}},
        _web_payload([_FINDING]),
    ]
    assert web_search_results(results) == [results[1]["payload"]]


def test_an_empty_search_is_not_worth_an_llm_call() -> None:
    assert web_search_results([_web_payload([])]) == []


# --- the dispatch ----------------------------------------------------------


async def test_a_turn_that_searched_queues_a_harvest() -> None:
    events = _run_events = await _run([_web_payload([_FINDING])])
    (harvest,) = _harvests(_run_events)
    assert harvest.user_id == "u-1"
    assert harvest.result["query"] == "full moon party dates"
    assert events  # the turn also dispatched TurnCompleted


async def test_a_turn_that_did_not_search_queues_nothing() -> None:
    events = await _run([{"tool": "find_saved", "payload": {"candidates": []}}])
    assert _harvests(events) == []


async def test_an_empty_search_queues_nothing() -> None:
    assert _harvests(await _run([_web_payload([])])) == []


async def test_the_config_gate_switches_the_write_back_off() -> None:
    """A bad harvest must be stoppable without a deploy."""
    events = await _run([_web_payload([_FINDING])], harvest_enabled=False)
    assert _harvests(events) == []


async def test_the_turn_still_completes_normally() -> None:
    """The harvest rides alongside TurnCompleted; it must not displace it."""
    events = await _run([_web_payload([_FINDING])])
    assert any(e.event_type == "turn_completed" for e in events)


async def test_a_web_only_turn_is_not_intent_bearing() -> None:
    """`web_search` returns no places, so a search-only turn must not enter
    the home "what you wanted" recall list (ADR-110)."""
    events = await _run([_web_payload([_FINDING])])
    (turn,) = [e for e in events if e.event_type == "turn_completed"]
    assert turn.surfaced_places is False
