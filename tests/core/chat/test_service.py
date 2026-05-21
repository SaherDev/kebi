"""Unit tests for ChatService (ADR-065 — agent-only path)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.chat.service import ChatService


def _make_service(
    taste_service: AsyncMock | None = None,
    memory_service: AsyncMock | None = None,
    agent_graph: AsyncMock | None = None,
) -> ChatService:
    """Build a ChatService with all deps mocked."""
    from kebi.core.config import get_config

    config = get_config()
    cfg_copy = config.model_copy(deep=True)

    if taste_service is None:
        taste_service = AsyncMock()
        taste_service.get_taste_profile = AsyncMock(return_value=None)
    if memory_service is None:
        memory_service = AsyncMock()
        memory_service.load_memories = AsyncMock(return_value=[])
    if agent_graph is None:
        graph = AsyncMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "messages": [AIMessage(content="default response")],
                "reasoning_steps": [],
            }
        )
        agent_graph = graph

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    return ChatService(
        event_dispatcher=dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=cfg_copy,
        agent_graph=agent_graph,
    )


async def test_run_invokes_agent_graph_and_returns_agent_type() -> None:
    """ChatService.run() invokes the agent graph and returns type='agent'."""
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="here's what I found")],
            "reasoning_steps": [],
        }
    )
    service = _make_service(agent_graph=graph)

    result = await service.run(ChatRequest(user_id="u1", message="show me my saves"))

    assert result.type == "agent"
    assert result.message == "here's what I found"
    graph.ainvoke.assert_awaited_once()


async def test_run_filters_reasoning_steps_to_user_visible() -> None:
    """Only user-visible ReasoningStep objects survive the serialization filter."""
    user_step = ReasoningStep(
        step="agent.tool_decision",
        summary="responding directly",
        source="agent",
        visibility="user",
    )
    debug_step = ReasoningStep(
        step="max_errors_detail",
        summary="exceeded max_errors",
        source="fallback",
        visibility="debug",
    )

    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content="response")],
            "reasoning_steps": [user_step, debug_step],
        }
    )
    service = _make_service(agent_graph=graph)

    result = await service.run(ChatRequest(user_id="u1", message="hi"))

    assert result.data is not None
    assert len(result.data["reasoning_steps"]) == 1
    assert result.data["reasoning_steps"][0]["step"] == "agent.tool_decision"


async def test_run_passes_user_id_as_thread_id() -> None:
    """graph.ainvoke is called with configurable.thread_id == request.user_id."""
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="ok")], "reasoning_steps": []}
    )
    service = _make_service(agent_graph=graph)

    await service.run(ChatRequest(user_id="u-agent", message="test"))

    call = graph.ainvoke.call_args
    assert call.kwargs["config"]["configurable"]["thread_id"] == "u-agent"


async def test_run_returns_error_on_graph_exception() -> None:
    """Unexpected exception during ainvoke surfaces as type='error'."""
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    service = _make_service(agent_graph=graph)
    result = await service.run(ChatRequest(user_id="u", message="hi"))

    assert result.type == "error"


# ---------------------------------------------------------------------------
# location passthrough — the request location is the user's actual location
# ---------------------------------------------------------------------------


async def test_run_threads_raw_user_location() -> None:
    """Raw lat/lng is threaded into the agent payload as `user_location` —
    the request location is the user's actual location, the resolution
    anchor for the resolve_location node."""
    from kebi.api.schemas.chat import Location

    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="ok")], "reasoning_steps": []}
    )

    service = _make_service(agent_graph=graph)

    await service.run(
        ChatRequest(
            user_id="u",
            message="ramen for date night",
            location=Location(lat=52.12, lng=11.62),
        )
    )

    payload = graph.ainvoke.call_args.args[0]
    assert payload["user_location"] == {"lat": 52.12, "lng": 11.62}


async def test_run_no_location_threads_none() -> None:
    """No location in request → payload user_location is None."""
    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="ok")], "reasoning_steps": []}
    )

    service = _make_service(agent_graph=graph)

    await service.run(ChatRequest(user_id="u", message="hi"))

    payload = graph.ainvoke.call_args.args[0]
    assert payload["user_location"] is None
