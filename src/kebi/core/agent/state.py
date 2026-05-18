"""AgentState TypedDict for the LangGraph agent (feature 027 M3, ADR-062).

LangGraph's StateGraph requires TypedDict (not Pydantic). `messages` uses
the `add_messages_capped` reducer so conversation history accumulates
across turns but is bounded by `agent.state_message_cap` to keep the
checkpointer blob from growing without limit. Every other field has
plain-overwrite semantics (FR-021).

`reasoning_steps` resets on every turn via `build_turn_payload` in
invocation.py — see that module for the single construction site.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from kebi.core.agent.reasoning import ReasoningStep

logger = logging.getLogger(__name__)


def add_messages_capped(left: Any, right: Any) -> list[BaseMessage]:
    """Append-and-cap reducer for `state["messages"]`.

    Wraps LangGraph's `add_messages` (handles dedup, RemoveMessage, etc.),
    then trims the front of the list when it exceeds `state_message_cap`,
    cutting at the next HumanMessage boundary at or after the floor.
    Cutting at HumanMessage keeps complete turns intact and never splits
    an AIMessage(tool_calls)/ToolMessage pair.

    Trimmed history is GONE from state — and therefore gone from the
    checkpointer blob on the next write. This is fine: the LLM only ever
    sees the last `max_history_messages` (40) messages, so anything older
    than `state_message_floor` (150) was already invisible to it. The
    floor-< -cap-and-floor-≥-window invariants are enforced in
    AgentConfig validation.
    """
    # `add_messages` returns a wider union type than list[BaseMessage] — at
    # runtime it always produces a list of BaseMessage when both args are
    # lists/messages, which is the only contract LangGraph reducers need.
    merged = cast(list[BaseMessage], add_messages(left, right))

    # Lazy import to avoid a circular dependency at module import time.
    from kebi.core.config import get_config

    cfg = get_config().agent
    cap = cfg.state_message_cap
    if len(merged) <= cap:
        return merged

    floor = cfg.state_message_floor
    cut_from = len(merged) - floor
    for i in range(cut_from, len(merged)):
        if isinstance(merged[i], HumanMessage):
            trimmed = merged[i:]
            logger.info(
                "Capped state[messages]: dropped %d, kept %d (cap=%d, floor=%d)",
                len(merged) - len(trimmed),
                len(trimmed),
                cap,
                floor,
            )
            return trimmed

    # No HumanMessage in the trim window — leave untouched. This shouldn't
    # happen in practice: every turn starts with a HumanMessage from
    # build_turn_payload, so a window of `floor` messages always contains
    # at least one. Logging at WARNING so it surfaces if the assumption breaks.
    logger.warning(
        "state[messages] over cap (%d > %d) but no HumanMessage in trim window; "
        "skipping trim",
        len(merged),
        cap,
    )
    return merged


class AgentState(TypedDict):
    """Per-turn state flowing through the LangGraph agent.

    Fields:
      messages            — conversation history; `add_messages` reducer appends.
      taste_profile_summary — behavior-derived preference bullets (per turn).
      memory_summary      — user-stated facts (per turn).
      user_id             — immutable per turn; used as the checkpointer thread_id.
      location            — {lat, lng} or None.
      location_label      — "Magdeburg, Germany" or None; resolved server-side
                            via reverse-geocode cache so the agent can reason
                            about the user's city (coords alone are too low-
                            info for the LLM to reverse-geocode in its head).
      reasoning_steps     — agent trace; reset to [] on every new user
                            message; no reducer (plain overwrite, FR-021).
      steps_taken         — incremented by agent_node; bounds should_continue.
      error_count         — incremented by tool error handlers (M9); bounds
                            should_continue.
      tool_calls_used     — incremented by each tool node (success or failure);
                            emitted in the response for NestJS rate limiting.
    """

    messages: Annotated[list[BaseMessage], add_messages_capped]
    taste_profile_summary: str
    memory_summary: str
    user_id: str
    location: dict[str, float] | None
    location_label: str | None
    reasoning_steps: list[ReasoningStep]
    steps_taken: int
    error_count: int
    tool_calls_used: int
