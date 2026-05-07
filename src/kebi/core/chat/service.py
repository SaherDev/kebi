"""ChatService — dispatch conversational requests to the agent pipeline.

Feature 028 M11 (ADR-065): the legacy intent-router dispatch path
(classify_intent, ChatAssistantService, IntentParser) has been deleted.
`run()` always delegates to `_run_agent`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphInterrupt

from kebi.api.schemas.chat import ChatRequest, ChatResponse
from kebi.core.agent.invocation import build_turn_payload
from kebi.core.agent.messages import extract_text_content
from kebi.core.consult.service import ConsultService
from kebi.core.events.events import TurnCompleted
from kebi.core.extraction.service import ExtractionService
from kebi.core.recall.service import RecallService
from kebi.core.taste.regen import format_summary_for_agent
from kebi.core.taste.schemas import SummaryLine

if TYPE_CHECKING:
    from kebi.core.config import AppConfig
    from kebi.core.events.dispatcher import EventDispatcherProtocol
    from kebi.core.memory.service import UserMemoryService
    from kebi.core.places import PlacesService
    from kebi.core.taste.service import TasteModelService

logger = logging.getLogger(__name__)


class ChatService:
    """Unified chat entry point — delegates all traffic to the agent pipeline."""

    def __init__(
        self,
        extraction_service: ExtractionService,
        consult_service: ConsultService,
        recall_service: RecallService,
        event_dispatcher: EventDispatcherProtocol,
        memory_service: UserMemoryService,
        taste_service: TasteModelService,
        places_service: PlacesService,
        config: AppConfig,
        agent_graph: Any,
    ) -> None:
        self._extraction = extraction_service
        self._consult = consult_service
        self._recall = recall_service
        self._dispatcher = event_dispatcher
        self._memory = memory_service
        self._taste_service = taste_service
        self._places_service = places_service
        self._config = config
        self._agent_graph = agent_graph

    async def run(self, request: ChatRequest) -> ChatResponse:
        """Delegate to `_run_agent` — the only dispatch path (ADR-065)."""
        try:
            return await self._run_agent(request)
        except Exception as exc:
            logger.exception("ChatService.run failed: %s", exc)
            return ChatResponse(
                type="error",
                message="Something went wrong, please try again.",
                data={"detail": str(exc)},
            )

    async def _run_agent(self, request: ChatRequest) -> ChatResponse:
        """Invoke the compiled agent graph and map its final state to ChatResponse.

        Dispatches a TurnCompleted event in `finally` so the memory layer
        captures every turn — success, clarification, or error.
        """
        try:
            # Pre-agent prep runs in parallel so the cold-path geocode hides
            # behind the taste/memory reads we'd do anyway.
            taste_summary, memory_summary, location_label = await asyncio.gather(
                self._compose_taste_summary(request.user_id),
                self._compose_memory_summary(request.user_id),
                self._resolve_location_label(request),
            )

            payload = build_turn_payload(
                message=request.message,
                user_id=request.user_id,
                taste_profile_summary=taste_summary,
                memory_summary=memory_summary,
                location=(request.location.model_dump() if request.location else None),
                location_label=location_label,
            )

            graph_config = {
                "configurable": {"thread_id": request.user_id},
                "metadata": {"user_id": request.user_id},
            }
            try:
                final_state = await self._agent_graph.ainvoke(
                    payload, config=graph_config
                )
            except GraphInterrupt as interrupt:
                # LangGraph wraps NodeInterrupt payload as:
                #   interrupt.args[0] == [Interrupt(value=<payload>, ...)]
                # Direct GraphInterrupt construction passes args[0] as a plain dict.
                raw = interrupt.args[0] if interrupt.args else {}
                if isinstance(raw, list) and raw and hasattr(raw[0], "value"):
                    interrupt_val: dict[str, Any] = raw[0].value
                elif isinstance(raw, dict):
                    interrupt_val = raw
                else:
                    interrupt_val = {}
                candidates = (
                    interrupt_val.get("candidates", [])
                    if isinstance(interrupt_val, dict)
                    else []
                )
                name = (
                    candidates[0].get("place", {}).get("place_name", "this place")
                    if candidates
                    else "this place"
                )
                return ChatResponse(
                    type="clarification",
                    message=f"Low confidence on {name} — is this the place you meant?",
                    data={"interrupt": interrupt_val},
                )

            messages = final_state.get("messages", [])
            ai_message = _last_ai_message(messages)
            all_steps = final_state.get("reasoning_steps", [])
            user_steps = [s for s in all_steps if s.visibility == "user"]
            tool_results = _collect_current_turn_tool_results(messages)

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
                    "reasoning_steps": [s.model_dump(mode="json") for s in user_steps],
                    "tool_results": tool_results,
                },
                tool_calls_used=final_state.get("tool_calls_used", 0),
            )
        finally:
            await self._dispatcher.dispatch(
                TurnCompleted(
                    user_id=request.user_id,
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

    async def _resolve_location_label(self, request: ChatRequest) -> str | None:
        """Resolve the user's lat/lng to a "City, Country" label via the
        cache-or-fetch helper on PlacesService. Returns None when no
        location was supplied, the cache+geocoder both failed, or the
        coords don't resolve to a known locality."""
        if request.location is None:
            return None
        return await self._places_service.resolve_location_label(
            lat=request.location.lat,
            lng=request.location.lng,
        )


def _last_ai_message(messages: list[Any]) -> AIMessage | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            return m
    return None


def _parse_tool_message_payload(m: ToolMessage) -> dict[str, Any] | None:
    """Return a dict payload for the tool_result SSE frame.

    Our tool wrappers (consult/recall/save + `with_timeout`) always put
    a JSON string in `ToolMessage.content`. LangGraph's `ToolNode`, however,
    returns a plain error-string ToolMessage when the tool's argument
    schema fails Pydantic validation (that happens before our wrapper
    runs), producing `status="error"` and non-JSON content. Surface that
    as a structured error payload so the client sees something actionable
    instead of a bare `null`.
    """
    content = m.content if isinstance(m.content, str) else ""
    if getattr(m, "status", None) == "error":
        return {"error": "tool_call_failed", "message": content or "tool error"}
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"error": "non_json_content", "message": content[:500]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _collect_current_turn_tool_results(messages: list[Any]) -> list[dict[str, Any]]:
    """Extract structured tool-result payloads produced during the current turn.

    The checkpointer preserves conversation history across turns, so
    `messages` contains prior turns too. We walk from the end and stop at
    the most recent `HumanMessage` — everything after it belongs to this
    turn. `ToolMessage.content` carries the tool's `response.model_dump_json()`
    string, which we parse back into a dict for the client.

    When recall and consult both ran in the same turn, recall is suppressed —
    consult already merges saved + discovered results so the recall payload
    is redundant for the consumer.
    """
    current_turn: list[Any] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        current_turn.append(m)
    current_turn.reverse()

    raw: list[dict[str, Any]] = []
    for m in current_turn:
        if not isinstance(m, ToolMessage):
            continue
        raw.append(
            {
                "tool": getattr(m, "name", None),
                "tool_call_id": getattr(m, "tool_call_id", None),
                "payload": _parse_tool_message_payload(m),
            }
        )

    tool_names = {r["tool"] for r in raw}
    if "consult" in tool_names:
        raw = [r for r in raw if r["tool"] != "recall"]

    results = raw
    return results
