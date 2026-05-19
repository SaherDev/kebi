"""Tests that ChatService dispatches TurnCompleted on every turn."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest
from kebi.core.chat.service import ChatService
from kebi.core.events.events import TurnCompleted


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
    graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="here you go")],
            "reasoning_steps": [],
        }
    )
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    result = await service.run(ChatRequest(user_id="u-1", message="find ramen"))

    assert result.type == "agent"
    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, TurnCompleted)
    assert event.user_id == "u-1"
    assert event.user_message == "find ramen"


async def test_run_dispatches_turn_completed_on_outer_error() -> None:
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    dispatcher = _make_dispatcher()
    service = _make_service(agent_graph=graph, dispatcher=dispatcher)

    result = await service.run(ChatRequest(user_id="u-3", message="anything"))

    assert result.type == "error"
    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, TurnCompleted)
    assert event.user_id == "u-3"
    assert event.user_message == "anything"
