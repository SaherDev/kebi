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
from kebi.core.agent.entity_links import (
    build_entity_index,
    linkify,
    normalize_voice,
    turn_recommendation_id,
)
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.events.events import TurnCompleted, WebFindingsHarvestRequested
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

# Tools whose results are place candidates. Only these mark a turn as
# "surfaced places" for the recall list (ADR-110): a `research` result is
# insider notes, not places to go, so a research-only turn never enters
# the home "what you wanted" list.
# `discover_places` is no longer a tool (ADR-140), but the catalog floor
# inside `suggest_places` still stamps its candidates `source="discovered"`,
# and older checkpointed turns carry the name. Dropping it here would silently
# stop those turns counting as intent-bearing for the recall list.
_PLACE_TOOLS = frozenset(
    {"find_saved", "suggest_places", "discover_places", "find_known"}
)


def surfaced_place_results(tool_results: list[dict[str, Any]]) -> bool:
    """True when any captured tool result this turn came from a place tool."""
    return any(tr.get("tool") in _PLACE_TOOLS for tr in tool_results)


def web_search_results(tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The web-search payloads this turn produced, if any (ADR-145).

    Only ones that actually found something: a search that came back empty
    has nothing to mine, and dispatching it would spend an LLM call to
    discover that.
    """
    return [
        payload
        for tr in tool_results
        if tr.get("tool") == "web_search"
        and isinstance(payload := tr.get("payload"), dict)
        and payload.get("findings")
    ]


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

    async def run(
        self, request: ChatRequest, *, user_id: str, taste_enabled: bool = False
    ) -> ChatResponse:
        """Delegate to `_run_agent` — the only dispatch path (ADR-065).

        `user_id` and `taste_enabled` are passed explicitly by the route
        after gateway-identity verification — never read from the request
        body. `taste_enabled` is the plan-tier gate: when false the taste
        model is not composed into the turn (free tier gets no taste
        personalization).
        """
        try:
            return await self._run_agent(
                request, user_id=user_id, taste_enabled=taste_enabled
            )
        except Exception:
            logger.exception("ChatService.run failed")
            return ChatResponse(
                type="error",
                message="Something went wrong, please try again.",
                data=None,
            )

    async def _run_agent(
        self, request: ChatRequest, *, user_id: str, taste_enabled: bool = False
    ) -> ChatResponse:
        """Invoke the compiled agent graph and map its final state to ChatResponse.

        Dispatches a TurnCompleted event in `finally` so the memory layer
        captures every turn — success or error.

        Wrapped in a per-turn Langfuse trace (`chat_turn`) so every paid
        observation created inside (orchestrator, resolver, tool-side
        Voyage embed, candidate namer) nests under one parent and the
        total turn cost is sliceable by user and feature.
        """
        # Initialized before the try so the `finally` can always read it,
        # even if the agent stream raises before any tool ran (ADR-110).
        tool_results: list[dict[str, Any]] = []
        working_location: dict[str, Any] | None = None
        async with feature_trace(
            "chat",
            user_id,
            name="chat_turn",
            extra={"endpoint": "/v1/chat"},
        ):
            try:
                # Pre-agent prep runs in parallel. Taste compose is gated by
                # the plan tier — skip the read entirely when not entitled.
                taste, memory_summary = await asyncio.gather(
                    self.compose_taste(user_id) if taste_enabled else _empty_taste(),
                    self._compose_memory_summary(user_id),
                )
                taste_summary, taste_values = taste

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
                    local_time=request.local_time,
                    taste_values=taste_values,
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
                async with asyncio.timeout(_CHAT_WALL_CLOCK_SECONDS):
                    async for snapshot in self._agent_graph.astream(
                        payload, config=graph_config, stream_mode="values"
                    ):
                        final_state = snapshot
                        snap_tool_results = snapshot.get("tool_results") or []
                        if snap_tool_results:
                            tool_results = snap_tool_results
                        # The resolver writes the working location early in
                        # the turn; hold the last populated one so the entity
                        # index can link the area the answer is about.
                        snap_working = snapshot.get("working_location")
                        if isinstance(snap_working, dict) and snap_working:
                            working_location = snap_working

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

                # Chat renders text plus entity links and nothing else (ADR-136):
                # the raw tool payloads stay server-side and the place names in
                # the prose become `kebi://` links the client can resolve.
                message_text, entities = linkify(
                    normalize_voice(message_text),
                    build_entity_index(tool_results, working_location),
                )

                return ChatResponse(
                    type="agent",
                    message=message_text,
                    data={
                        # `id`/`status` are SSE step-lifecycle markers (ADR-102)
                        # — they're set only on stream frames and excluded here
                        # so the non-stream JSON contract stays unchanged.
                        "reasoning_steps": [
                            s.model_dump(mode="json", exclude={"id", "status"})
                            for s in user_steps
                        ],
                        "entities": [e.model_dump(mode="json") for e in entities],
                        "recommendation_id": turn_recommendation_id(tool_results),
                    },
                    tool_calls_used=final_state.get("tool_calls_used", 0),
                )
            finally:
                # A turn that surfaced place results is intent-bearing — the
                # free signal that gates the recall list (ADR-110).
                await self._dispatcher.dispatch(
                    TurnCompleted(
                        user_id=user_id,
                        user_message=request.message,
                        surfaced_places=surfaced_place_results(tool_results),
                    )
                )
                # Web findings are mined into durable claims after the answer
                # is sent (ADR-145), so the lookup this user paid for makes
                # the next person's question free. Same `finally` as
                # TurnCompleted: enrichment never blocks the response.
                if self._config.agent.web_search.harvest_enabled:
                    for result in web_search_results(tool_results):
                        await self._dispatcher.dispatch(
                            WebFindingsHarvestRequested(user_id=user_id, result=result)
                        )

    async def _compose_taste_summary(self, user_id: str) -> str:
        lines = await self._taste_lines(user_id)
        return format_summary_for_agent(lines) if lines else ""

    async def _taste_lines(self, user_id: str) -> list[SummaryLine]:
        profile = await self._taste_service.get_taste_profile(user_id)
        if profile is None or not profile.taste_profile_summary:
            return []
        return [
            SummaryLine.model_validate(item) if isinstance(item, dict) else item
            for item in profile.taste_profile_summary
        ]

    async def compose_taste(self, user_id: str) -> tuple[str, list[str]]:
        """Both halves of the taste signal from ONE read (ADR-142).

        The prose summary goes in the prompt; the vocabulary values behind it
        go to retrieval, because `format_summary_for_agent` drops
        `source_value` and that is the only part a ranker can match on. Derived
        together deliberately — asking the taste service twice per turn for two
        views of the same rows is a second round-trip for nothing.
        """
        lines = await self._taste_lines(user_id)
        if not lines:
            return "", []
        seen: list[str] = []
        for line in lines:
            value = (line.source_value or "").strip()
            if value and value.lower() not in {v.lower() for v in seen}:
                seen.append(value)
        return format_summary_for_agent(lines), seen

    async def _compose_memory_summary(self, user_id: str) -> str:
        memory_list = await self._memory.load_memories(user_id)
        if not memory_list:
            return ""
        return "\n".join(memory_list)


async def _empty_summary() -> str:
    """Awaitable stand-in for a skipped compose, so the gather stays uniform."""
    return ""


async def _empty_taste() -> tuple[str, list[str]]:
    """Same, for the plan tier that gets no taste personalisation."""
    return "", []


def _last_ai_message(messages: list[Any]) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None
