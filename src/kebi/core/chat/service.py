"""ChatService — dispatch conversational requests to the agent pipeline.

Feature 028 M11 (ADR-065): the legacy intent-router dispatch path
(classify_intent, ChatAssistantService, IntentParser) has been deleted.
`run()` always delegates to `_run_agent`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage

from kebi.api.schemas.chat import ChatRequest, ChatResponse
from kebi.core.agent._trace_context import feature_trace
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.events.events import TurnCompleted
from kebi.core.taste.regen import format_summary_for_agent
from kebi.core.taste.schemas import SummaryLine

if TYPE_CHECKING:
    from kebi.core.config import AppConfig
    from kebi.core.events.dispatcher import EventDispatcherProtocol
    from kebi.core.memory.service import UserMemoryService
    from kebi.core.taste.service import TasteModelService

logger = logging.getLogger(__name__)

# Hard wall-clock on the synchronous chat path. Mirrors the SSE budget
# in `api/routes/chat.py`. Per-tool budgets bound individual nodes;
# this bounds the whole turn so a misbehaving graph or a hung
# downstream can't pin the worker indefinitely.
_CHAT_WALL_CLOCK_SECONDS = 90.0


class ChatService:
    """Unified chat entry point — delegates all traffic to the agent pipeline."""

    def __init__(
        self,
        event_dispatcher: EventDispatcherProtocol,
        memory_service: UserMemoryService,
        taste_service: TasteModelService,
        config: AppConfig,
        agent_graph: Any,
    ) -> None:
        self._dispatcher = event_dispatcher
        self._memory = memory_service
        self._taste_service = taste_service
        self._config = config
        self._agent_graph = agent_graph

    async def run(self, request: ChatRequest, *, user_id: str) -> ChatResponse:
        """Delegate to `_run_agent` — the only dispatch path (ADR-065).

        `user_id` is passed explicitly by the route after gateway-identity
        verification — never read from the request body.
        """
        try:
            return await self._run_agent(request, user_id=user_id)
        except Exception:
            logger.exception("ChatService.run failed")
            return ChatResponse(
                type="error",
                message="Something went wrong, please try again.",
                data=None,
            )

    async def _run_agent(
        self, request: ChatRequest, *, user_id: str
    ) -> ChatResponse:
        """Invoke the compiled agent graph and map its final state to ChatResponse.

        Dispatches a TurnCompleted event in `finally` so the memory layer
        captures every turn — success or error.

        Wrapped in a per-turn Langfuse trace (`chat_turn`) so every paid
        observation created inside (orchestrator, resolver, tool-side
        Voyage embed, candidate namer) nests under one parent and the
        total turn cost is sliceable by user and feature.
        """
        async with feature_trace(
            "chat",
            user_id,
            name="chat_turn",
            extra={"endpoint": "/v1/chat"},
        ):
            try:
                # Pre-agent prep runs in parallel.
                taste_summary, memory_summary = await asyncio.gather(
                    self._compose_taste_summary(user_id),
                    self._compose_memory_summary(user_id),
                )

                payload = build_turn_payload(
                    message=request.message,
                    user_id=user_id,
                    taste_profile_summary=taste_summary,
                    memory_summary=memory_summary,
                    user_location=(
                        request.location.model_dump() if request.location else None
                    ),
                    movement_profile=(
                        request.movement_profile.model_dump()
                        if request.movement_profile
                        else None
                    ),
                )

                graph_config = {
                    "configurable": {"thread_id": user_id},
                    "metadata": {"user_id": user_id},
                }
                # The only producer of GraphInterrupt was the save tool's
                # needs_review branch (ADR-063). ADR-071 removed that branch
                # and ADR-073 removed the save tool entirely, so the agent
                # can no longer raise GraphInterrupt — no handler needed.
                #
                # Stream values instead of ainvoke so we can capture
                # `tool_results` from the snapshot emitted between
                # `finalize` and `scrub_tool_results`. The final state we
                # checkpoint has `tool_results=[]` — only `reasoning_steps`
                # (human-readable summaries) persist as agent history.
                final_state: dict[str, Any] = {}
                tool_results: list[dict[str, Any]] = []
                async with asyncio.timeout(_CHAT_WALL_CLOCK_SECONDS):
                    async for snapshot in self._agent_graph.astream(
                        payload, config=graph_config, stream_mode="values"
                    ):
                        final_state = snapshot
                        snap_tool_results = snapshot.get("tool_results") or []
                        if snap_tool_results:
                            tool_results = snap_tool_results

                messages = final_state.get("messages", [])
                ai_message = _last_ai_message(messages)
                all_steps = final_state.get("reasoning_steps", [])
                user_steps = [s for s in all_steps if s.visibility == "user"]

                message_text = (
                    extract_text_content(ai_message.content) if ai_message else ""
                ).strip()
                if not message_text:
                    # Tool-use-only AIMessage or no response at all — give the client
                    # something renderable rather than an empty bubble.
                    message_text = "I'm working on it."

                return ChatResponse(
                    type="agent",
                    message=message_text,
                    data={
                        "reasoning_steps": [
                            s.model_dump(mode="json") for s in user_steps
                        ],
                        "tool_results": tool_results,
                    },
                    tool_calls_used=final_state.get("tool_calls_used", 0),
                )
            finally:
                await self._dispatcher.dispatch(
                    TurnCompleted(
                        user_id=user_id,
                        user_message=request.message,
                    )
                )

    async def _compose_taste_summary(self, user_id: str) -> str:
        profile = await self._taste_service.get_taste_profile(user_id)
        if profile is None or not profile.taste_profile_summary:
            return ""
        lines = [
            SummaryLine.model_validate(item) if isinstance(item, dict) else item
            for item in profile.taste_profile_summary
        ]
        return format_summary_for_agent(lines)

    async def _compose_memory_summary(self, user_id: str) -> str:
        memory_list = await self._memory.load_memories(user_id)
        if not memory_list:
            return ""
        return "\n".join(memory_list)


def _last_ai_message(messages: list[Any]) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


