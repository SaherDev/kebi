"""POST /v1/chat and POST /v1/chat/stream — unified chat entry point (ADR-052).

Note: this file intentionally does NOT use `from __future__ import
annotations`. slowapi's `@limiter.limit` wraps the route function and
FastAPI's per-body type-adapter rebuild then fails to resolve forward
refs from the wrapper's module globals — manifests as
`PydanticUserError: ChatRequest is not fully defined`. Keeping
annotations as real classes here sidesteps that interaction.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage

from kebi.api.deps import (
    GatewayIdentity,
    get_agent_graph,
    get_chat_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.chat import ChatRequest, ChatResponse
from kebi.core.agent._trace_context import feature_trace
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.chat.service import ChatService
from kebi.core.config import get_env
from kebi.core.events.events import TurnCompleted
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)

# Hard wall-clock budget on a single SSE stream. The agent's per-tool
# budgets (`agent.tool_timeouts_seconds`) bound individual nodes; this
# bounds the whole turn. Anti-slowloris: a client that opens the
# connection then refuses to read can't pin a worker forever — once we
# exceed the budget we emit a `timeout` error frame and close.
_SSE_WALL_CLOCK_SECONDS = 90.0

# Hard cap on a single SSE frame's `data:` JSON body. Bounds memory if
# a tool result or reasoning step ever becomes unexpectedly large. The
# limit is high (16 KiB) so normal frames pass through untouched.
_SSE_FRAME_MAX_BYTES = 16 * 1024


def _frame(event: str, payload: dict[str, Any] | str) -> str:
    """Format an SSE frame, truncating the JSON body if it overflows."""
    body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if len(body.encode("utf-8")) > _SSE_FRAME_MAX_BYTES:
        body = json.dumps(
            {"truncated": True, "original_bytes": len(body.encode("utf-8"))}
        )
    return f"event: {event}\ndata: {body}\n\n"


router = APIRouter()


@router.post("/chat", status_code=200)
@limiter.limit("30/minute")
async def chat(
    request: Request,
    body: ChatRequest = Body(...),  # noqa: B008
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    service: ChatService = Depends(get_chat_service),  # noqa: B008
) -> ChatResponse:
    """Unified chat endpoint — classify intent and dispatch to correct pipeline.

    Args:
        body: Chat request containing message, optional location, optional
            movement profile. `user_id` is NOT a body field — it is
            forwarded by the gateway as `X-Gateway-User-Id` and resolved
            via `require_gateway_identity`.
        identity: Verified gateway identity (carries `user_id`).
        service: Injected ChatService instance.

    Returns:
        ChatResponse with type, message, and optional data payload.
    """
    return await service.run(body, user_id=identity.user_id)


@router.post("/chat/stream", status_code=200)
@limiter.limit("30/minute")
async def chat_stream(
    request: Request,
    body: ChatRequest = Body(...),  # noqa: B008
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    service: ChatService = Depends(get_chat_service),  # noqa: B008
    agent_graph: Any = Depends(get_agent_graph),  # noqa: B008
) -> StreamingResponse:
    """SSE streaming chat endpoint — emits reasoning_step frames then final message.

    Requires agent path to be enabled and graph to be available.
    Returns 400 if agent is disabled or graph is unavailable.

    Frame format (text/event-stream). Each reasoning step is streamed twice
    over its lifecycle (ADR-102), keyed by a stable `id`: an `active` frame
    when it starts (`summary`/`duration_ms` null → the client shows a
    skeleton) then a `done` frame when it completes (same `id`, filled in):
      event: reasoning_step
      data: {"id":"find_saved#0","step":"find_saved","summary":null,
             "status":"active","source":"agent","visibility":"user",
             "duration_ms":null}

      event: reasoning_step
      data: {"id":"find_saved#0","step":"find_saved.summary",
             "summary":"Found 2 saved spots — …","status":"done",
             "source":"agent","visibility":"user","duration_ms":420.0}

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

    user_id = identity.user_id
    taste_summary = await service._compose_taste_summary(user_id)
    memory_summary = await service._compose_memory_summary(user_id)

    payload = build_turn_payload(
        message=body.message,
        user_id=user_id,
        taste_profile_summary=taste_summary,
        memory_summary=memory_summary,
        user_location=(body.location.model_dump() if body.location else None),
        movement_profile=(
            body.movement_profile.model_dump() if body.movement_profile else None
        ),
    )
    graph_config = {
        "configurable": {"thread_id": user_id},
        "metadata": {"user_id": user_id},
    }

    async def generate() -> AsyncGenerator[str, None]:
        async with feature_trace(
            "chat",
            user_id,
            name="chat_turn",
            extra={"endpoint": "/v1/chat/stream"},
        ):
            tracer = get_tracing_client()
            final_state: dict[str, Any] = {}
            # Tool result payloads live on state for one superstep — between
            # `finalize` (which populates them) and `scrub_tool_results`
            # (which clears them so they never reach the checkpointer DB).
            # Capture the populated snapshot here; the final `final_state`
            # we read after the loop has `tool_results=[]` by design.
            tool_results: list[dict[str, Any]] = []
            try:
                try:
                    # Hard wall-clock bound: a slow-reading or unresponsive
                    # client can't pin the worker beyond _SSE_WALL_CLOCK_SECONDS.
                    async with asyncio.timeout(_SSE_WALL_CLOCK_SECONDS):
                        async for stream_mode, chunk in agent_graph.astream(
                            payload,
                            config=graph_config,
                            stream_mode=["custom", "values"],
                        ):
                            if await request.is_disconnected():
                                tracer.capture_message(
                                    message="chat_stream client disconnected",
                                    level="info",
                                    metadata={"user_id": user_id},
                                    user_id=user_id,
                                )
                                return
                            if stream_mode == "custom":
                                yield _frame("reasoning_step", chunk)
                            elif stream_mode == "values":
                                final_state = chunk
                                snap_tool_results = chunk.get("tool_results") or []
                                if snap_tool_results:
                                    tool_results = snap_tool_results
                except TimeoutError:
                    logger.warning(
                        "chat_stream wall-clock timeout (%.0fs) for user %s",
                        _SSE_WALL_CLOCK_SECONDS,
                        user_id,
                    )
                    yield _frame("error", {"detail": "timeout"})
                    return
                except Exception:
                    logger.exception("chat_stream graph error")
                    yield _frame("error", {"detail": "internal_error"})
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

                for tool_result in tool_results:
                    yield _frame("tool_result", tool_result)
                if final_message:
                    yield _frame("message", {"content": final_message})
                yield _frame("done", {"tool_calls_used": tool_calls_used})
            finally:
                await service._dispatcher.dispatch(
                    TurnCompleted(user_id=user_id, user_message=body.message)
                )

    return StreamingResponse(generate(), media_type="text/event-stream")
