"""Tests that ChatService dispatches TurnCompleted on every turn."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest
from kebi.core.chat.service import ChatService
from kebi.core.events.events import TurnCompleted


def _mock_astream(
    values: list[dict[str, Any]] | None = None,
    *,
    raises: Exception | None = None,
) -> MagicMock:
    """Same shape as the helper in test_service.py — stub `graph.astream`."""
    snapshots = values or [{"messages": [], "reasoning_steps": []}]

    def _factory(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            if raises is not None:
                raise raises
            for v in snapshots:
                yield v

        return _gen()

    return MagicMock(side_effect=_factory)


def _make_service(agent_graph: MagicMock, dispatcher: MagicMock) -> ChatService:
    from kebi.core.config import get_config

    cfg = get_config().model_copy(deep=True)

    taste_service = AsyncMock()
    taste_service.get_taste_profile = AsyncMock(return_value=None)
    memory_service = AsyncMock()
    memory_service.load_memories = AsyncMock(return_value=[])

    return ChatService(
        event_dispatcher=dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=cfg,
        agent_graph=agent_graph,
    )


def _make_dispatcher() -> MagicMock:
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    return dispatcher


async def test_run_dispatches_turn_completed_on_success() -> None:
    graph = AsyncMock()
    graph.astream = _mock_astream(
        [
            {
                "messages": [AIMessage(content="here you go")],
                "reasoning_steps": [],
            }
        ]
    )
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    result = await service.run(ChatRequest(message="find ramen"), user_id="u-1")

    assert result.type == "agent"
    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, TurnCompleted)
    assert event.user_id == "u-1"
    assert event.user_message == "find ramen"


async def test_place_tool_results_mark_turn_as_surfaced() -> None:
    graph = AsyncMock()
    graph.astream = _mock_astream(
        [
            {
                "messages": [AIMessage(content="here you go")],
                "reasoning_steps": [],
                "tool_results": [
                    {"tool": "find_saved", "tool_call_id": "tc-1", "payload": {}}
                ],
            }
        ]
    )
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    await service.run(ChatRequest(message="find ramen"), user_id="u-1")

    event = dispatcher.dispatch.await_args.args[0]
    assert event.surfaced_places is True


async def test_research_only_turn_not_marked_as_surfaced() -> None:
    """A research turn returns insider notes, not places to go — it must
    not enter the ADR-110 home recall list."""
    graph = AsyncMock()
    graph.astream = _mock_astream(
        [
            {
                "messages": [AIMessage(content="locals use BIDV")],
                "reasoning_steps": [],
                "tool_results": [
                    {"tool": "research", "tool_call_id": "tc-1", "payload": {}}
                ],
            }
        ]
    )
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    await service.run(ChatRequest(message="atm fees in da nang?"), user_id="u-2")

    event = dispatcher.dispatch.await_args.args[0]
    assert event.surfaced_places is False


async def test_mixed_research_and_place_turn_counts_as_surfaced() -> None:
    graph = AsyncMock()
    graph.astream = _mock_astream(
        [
            {
                "messages": [AIMessage(content="here you go")],
                "reasoning_steps": [],
                "tool_results": [
                    {"tool": "research", "tool_call_id": "tc-1", "payload": {}},
                    {"tool": "suggest_places", "tool_call_id": "tc-2", "payload": {}},
                ],
            }
        ]
    )
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    await service.run(ChatRequest(message="cash near me"), user_id="u-4")

    event = dispatcher.dispatch.await_args.args[0]
    assert event.surfaced_places is True


async def test_run_dispatches_turn_completed_on_outer_error() -> None:
    graph = AsyncMock()
    graph.astream = _mock_astream(raises=RuntimeError("boom"))
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    result = await service.run(ChatRequest(message="anything"), user_id="u-3")

    assert result.type == "error"
    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, TurnCompleted)
    assert event.user_id == "u-3"
    assert event.user_message == "anything"
