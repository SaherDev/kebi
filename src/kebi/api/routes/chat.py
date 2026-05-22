"""POST /v1/chat and POST /v1/chat/stream — unified chat entry point (ADR-052)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage

from kebi.api.deps import get_agent_graph, get_chat_service
from kebi.api.schemas.chat import ChatRequest, ChatResponse
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.chat.service import ChatService, _collect_current_turn_tool_results
from kebi.core.config import get_env
from kebi.core.events.events import TurnCompleted
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", status_code=200)
async def chat(
    body: ChatRequest,
    service: ChatService = Depends(get_chat_service),  # noqa: B008
) -> ChatResponse:
    """Unified chat endpoint — classify intent and dispatch to correct pipeline.

    Args:
        body: Chat request containing user_id, message, and optional location.
        service: Injected ChatService instance.

    Returns:
        ChatResponse with type, message, and optional data payload.
    """
    return await service.run(body)


@router.post("/chat/stream", status_code=200)
async def chat_stream(
    body: ChatRequest,
    request: Request,
    service: ChatService = Depends(get_chat_service),  # noqa: B008
    agent_graph: Any = Depends(get_agent_graph),  # noqa: B008
) -> StreamingResponse:
    """SSE streaming chat endpoint — emits reasoning_step frames then final message.

    Requires agent path to be enabled and graph to be available.
    Returns 400 if agent is disabled or graph is unavailable.

    Frame format (text/event-stream):
      event: reasoning_step
      data: <ReasoningStep JSON>

      event: message
      data: {"content": "<final assistant text>"}

    Args:
        body: Chat request containing user_id, message, and optional location.
        request: FastAPI request (used for agent_graph via app.state).
        service: Injected ChatService (provides taste/memory helpers).
        agent_graph: Compiled LangGraph StateGraph from app.state.

    Returns:
        StreamingResponse with text/event-stream content type.
    """
    from fastapi.responses import JSONResponse

    if agent_graph is None or not get_env().AGENT_ENABLED:
        return JSONResponse(  # type: ignore[return-value]
            status_code=400,
            content={"detail": "Agent not enabled or graph unavailable"},
        )

    taste_summary = await service._compose_taste_summary(body.user_id)
    memory_summary = await service._compose_memory_summary(body.user_id)

    payload = build_turn_payload(
        message=body.message,
        user_id=body.user_id,
        taste_profile_summary=taste_summary,
        memory_summary=memory_summary,
        user_location=(body.location.model_dump() if body.location else None),
        movement_profile=(
            body.movement_profile.model_dump() if body.movement_profile else None
        ),
    )
    graph_config = {
        "configurable": {"thread_id": body.user_id},
        "metadata": {"user_id": body.user_id},
    }

    async def generate() -> AsyncGenerator[str, None]:
        final_state: dict[str, Any] = {}
        try:
            try:
                async for stream_mode, chunk in agent_graph.astream(
                    payload, config=graph_config, stream_mode=["custom", "values"]
                ):
                    if await request.is_disconnected():
                        get_tracing_client().capture_message(
                            message="chat_stream client disconnected",
                            level="info",
                            metadata={"user_id": body.user_id},
                            user_id=body.user_id,
                        )
                        return
                    if stream_mode == "custom":
                        data = json.dumps(chunk, default=str)
                        yield f"event: reasoning_step\ndata: {data}\n\n"
                    elif stream_mode == "values":
                        final_state = chunk
            except Exception as exc:
                logger.exception("chat_stream graph error: %s", exc)
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
                return

            messages: list[Any] = final_state.get("messages") or []
            tool_calls_used: int = final_state.get("tool_calls_used") or 0

            final_message = ""
            for m in reversed(messages):
                if isinstance(m, AIMessage):
                    text = extract_text_content(m.content)
                    if text:
                        final_message = text
                        break

            for tool_result in _collect_current_turn_tool_results(messages):
                yield f"event: tool_result\ndata: {json.dumps(tool_result)}\n\n"
            if final_message:
                msg_payload = json.dumps({"content": final_message})
                yield f"event: message\ndata: {msg_payload}\n\n"
            done_payload = json.dumps({"tool_calls_used": tool_calls_used})
            yield f"event: done\ndata: {done_payload}\n\n"
        finally:
            await service._dispatcher.dispatch(
                TurnCompleted(user_id=body.user_id, user_message=body.message)
            )

    return StreamingResponse(generate(), media_type="text/event-stream")
