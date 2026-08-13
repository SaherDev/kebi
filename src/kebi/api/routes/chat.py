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
    get_consult_quota_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.chat import ChatRequest, ChatResponse
from kebi.core.agent._trace_context import feature_trace
from kebi.core.agent.entity_links import (
    build_entity_index,
    linkify,
    normalize_voice,
    turn_recommendation_id,
)
from kebi.core.agent.graph import NODE_AGENT
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.chat.consult_quota import ConsultQuotaService
from kebi.core.chat.delta_buffer import DeltaBuffer
from kebi.core.chat.service import ChatService, surfaced_place_results
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
    quota: ConsultQuotaService = Depends(get_consult_quota_service),  # noqa: B008
) -> ChatResponse:
    """Unified chat endpoint — classify intent and dispatch to correct pipeline.

    Args:
        body: Chat request containing message, optional location, optional
            movement profile. `user_id` is NOT a body field — it is
            forwarded by the gateway as `X-Gateway-User-Id` and resolved
            via `require_gateway_identity`.
        identity: Verified gateway identity (carries `user_id` plus the
            plan-tier entitlements: `consults_per_day` quota and
            `taste_enabled`).
        service: Injected ChatService instance.
        quota: Redis-backed daily consult quota enforcer.

    Returns:
        ChatResponse with type, message, and optional data payload. When the
        daily consult quota is exhausted, an `error` response carrying
        `data={"reason": "daily_limit_reached"}` so the gateway can surface
        the upgrade prompt.
    """
    if not await quota.check_and_increment(identity.user_id, identity.consults_per_day):
        return ChatResponse(
            type="error",
            message="You've reached today's consult limit.",
            data={"reason": "daily_limit_reached"},
        )
    return await service.run(
        body, user_id=identity.user_id, taste_enabled=identity.taste_enabled
    )


@router.post("/chat/stream", status_code=200)
@limiter.limit("30/minute")
async def chat_stream(
    request: Request,
    body: ChatRequest = Body(...),  # noqa: B008
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    service: ChatService = Depends(get_chat_service),  # noqa: B008
    agent_graph: Any = Depends(get_agent_graph),  # noqa: B008
    quota: ConsultQuotaService = Depends(get_consult_quota_service),  # noqa: B008
) -> StreamingResponse:
    """SSE streaming chat endpoint — emits reasoning_step frames then final message.

    Requires agent path to be enabled and graph to be available.
    Returns 400 if agent is disabled or graph is unavailable.

    Frame format (text/event-stream). Each reasoning step is streamed twice
    over its lifecycle (ADR-102), keyed by a stable `id`: an `active` frame
    when it starts (`summary`/`duration_ms` null → the client shows a
    skeleton) then a `done` frame when it completes (same `id`, filled in).
    Each step carries two human fields (ADR-103): `title` is the bold action
    line (same on both frames), `summary` is the result line under it (null
    on `active`, filled on `done`):
      event: reasoning_step
      data: {"id":"find_saved#0","step":"find_saved","title":"searched your
             saved spots","summary":null,"status":"active","source":"agent",
             "visibility":"user","duration_ms":null}

      event: reasoning_step
      data: {"id":"find_saved#0","step":"find_saved.summary","title":"searched
             your saved spots","summary":"2 spots — Wagyu, Beef Tei",
             "status":"done","source":"agent","visibility":"user",
             "duration_ms":420.0}

    The orchestrator's own words stream live (ADR-158/159). While a
    message's kind is still unknown, its text types into the active
    thinking row as `reasoning_delta` frames (keyed by the step's `id`);
    if the message turns out to be the answer, the first `message_delta`
    carries `promote: true` — the client clears the thinking row's typed
    text and seeds the answer bubble with that delta's text (the full
    normalized prefix, so nothing is lost), then appends the rest. A
    message that ends in a tool call keeps its text in the thinking row,
    where the step's `done` frame supersedes it.

      event: reasoning_delta
      data: {"id": "agent.tool_decision#1", "text": "checking what you"}

      event: message_delta
      data: {"text": "tonight is Luigi's night, the counter", "promote": true}

      event: message_delta
      data: {"text": " seats are the move"}

    The terminal `message` frame carries the answer text with entity names
    already wrapped as markdown links to `kebi://{kind}/{key}` URIs, plus the
    flat `entities` list resolving each one (ADR-136). It is authoritative:
    a client that rendered deltas replaces them with this content wholesale
    (same words — the swap just makes the links tappable), so a link can
    never be split across frames. Clients that ignore `message_delta`
    behave exactly as before. There are no `tool_result` frames: chat
    renders text and links, and every richer view lives on a detail screen
    the link opens.

      event: message
      data: {"content": "tonight is [Luigi's](kebi://venue/9f3…) night",
             "entities": [{"kind":"venue","key":"9f3…","name":"Luigi's",
                           "uri":"kebi://venue/9f3…"}]}

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

    # Daily consult quota — checked before any taste read or agent work, so a
    # maxed user spends nothing. Short-circuit with a terminal SSE error so
    # the client stays on the event-stream contract (mirrors the structured
    # `daily_limit_reached` reason on the non-stream path).
    if not await quota.check_and_increment(user_id, identity.consults_per_day):

        async def _limited() -> AsyncGenerator[str, None]:
            yield _frame("error", {"detail": "daily_limit_reached"})
            yield _frame("done", {"tool_calls_used": 0})

        return StreamingResponse(_limited(), media_type="text/event-stream")

    # Taste compose is plan-gated (free tier gets no taste personalization).
    taste_summary, taste_values = (
        await service.compose_taste(user_id) if identity.taste_enabled else ("", [])
    )
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
        user_profile=(body.user_profile.model_dump() if body.user_profile else None),
        local_time=body.local_time,
        taste_values=taste_values,
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
            # Same capture-the-last-populated-snapshot rule: the resolver
            # writes `working_location` early in the turn, and the entity
            # index needs it to link the area the answer is about.
            working_location: Any = None
            # Token router (ADR-158/159): each agent LLM call's text
            # streams as narration into the active thinking row until it
            # proves to be the answer, then promotes to the answer bubble.
            delta_buffer = DeltaBuffer()
            # The thinking row narration deltas type into — the last
            # `agent.tool_decision` step to go active on the custom stream.
            thinking_step_id: str | None = None

            def _delta_frames(events: Any) -> list[str]:
                frames: list[str] = []
                for ev in events:
                    if ev.kind == "narration":
                        if thinking_step_id is None:
                            continue
                        frames.append(
                            _frame(
                                "reasoning_delta",
                                {"id": thinking_step_id, "text": ev.text},
                            )
                        )
                    else:
                        payload_out: dict[str, Any] = {"text": ev.text}
                        if ev.promote:
                            payload_out["promote"] = True
                        frames.append(_frame("message_delta", payload_out))
                return frames

            try:
                try:
                    # Hard wall-clock bound: a slow-reading or unresponsive
                    # client can't pin the worker beyond _SSE_WALL_CLOCK_SECONDS.
                    async with asyncio.timeout(_SSE_WALL_CLOCK_SECONDS):
                        async for stream_mode, chunk in agent_graph.astream(
                            payload,
                            config=graph_config,
                            stream_mode=["custom", "messages", "values"],
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
                                if (
                                    isinstance(chunk, dict)
                                    and chunk.get("step") == "agent.tool_decision"
                                    and chunk.get("status") == "active"
                                ):
                                    thinking_step_id = chunk.get("id")
                                yield _frame("reasoning_step", chunk)
                            elif stream_mode == "messages":
                                # Orchestrator token stream (ADR-158/159):
                                # narration types into the thinking row,
                                # answer prose streams to the bubble; links
                                # only ever ride the terminal `message`
                                # frame below. Other nodes' LLM calls
                                # (resolver, tool-internal) never stream.
                                msg_chunk, meta = chunk
                                if meta.get("langgraph_node") != NODE_AGENT:
                                    continue
                                events = delta_buffer.feed(
                                    getattr(msg_chunk, "id", None),
                                    extract_text_content(
                                        getattr(msg_chunk, "content", None)
                                    ),
                                    bool(getattr(msg_chunk, "tool_call_chunks", None)),
                                )
                                for out in _delta_frames(events):
                                    yield out
                            elif stream_mode == "values":
                                # A message still narrating at a superstep
                                # boundary ended with no tool call — it was
                                # the answer; promote it before the terminal
                                # `message` frame lands.
                                for out in _delta_frames(delta_buffer.boundary()):
                                    yield out
                                final_state = chunk
                                snap_tool_results = chunk.get("tool_results") or []
                                if snap_tool_results:
                                    tool_results = snap_tool_results
                                snap_working = chunk.get("working_location")
                                if isinstance(snap_working, dict) and snap_working:
                                    working_location = snap_working
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

                if final_message:
                    # Text plus entity links is the entire render contract
                    # (ADR-136) — the tool payloads that used to ride their own
                    # `tool_result` frames stay server-side.
                    final_message, entities = linkify(
                        normalize_voice(final_message),
                        build_entity_index(tool_results, working_location),
                    )
                    # Same row-sourced icons as the JSON path — the two
                    # chat paths must ship identical entities.
                    entities = await service.refresh_entity_icons(entities)
                    yield _frame(
                        "message",
                        {
                            "content": final_message,
                            "entities": [e.model_dump(mode="json") for e in entities],
                            "recommendation_id": turn_recommendation_id(tool_results),
                        },
                    )
                yield _frame("done", {"tool_calls_used": tool_calls_used})
            finally:
                # A turn that surfaced place results is intent-bearing — the
                # free signal that gates the recall list (ADR-110). Mirrors
                # the non-stream path in ChatService._run_agent.
                await service._dispatcher.dispatch(
                    TurnCompleted(
                        user_id=user_id,
                        user_message=body.message,
                        surfaced_places=surfaced_place_results(tool_results),
                    )
                )

    return StreamingResponse(generate(), media_type="text/event-stream")
