"""AgentState TypedDict for the LangGraph agent (feature 027 M3, ADR-062).

LangGraph's StateGraph requires TypedDict (not Pydantic). `messages` uses
the `add_messages_capped` reducer so conversation history accumulates
across turns but is bounded by `agent.state_message_cap` to keep the
checkpointer blob from growing without limit. Every other field has
plain-overwrite semantics (FR-021).

`reasoning_steps` and `location_clarification` reset on every turn via
`build_turn_payload` in invocation.py — see that module for the single
construction site.

`working_location` is the exception to plain-overwrite: it carries across
turns. `build_turn_payload` passes the `LOCATION_INHERIT` sentinel and the
`merge_working_location` reducer maps that to "keep the prior turn's value",
so the location the agent resolved last turn survives unless the
`resolve_location` node explicitly replaces it.
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


# Sentinel passed by `build_turn_payload` into `working_location`. The
# `merge_working_location` reducer maps it to "keep the value carried from
# the previous turn" — making carry-forward explicit instead of relying on
# key omission, which a future editor could silently break.
LOCATION_INHERIT = "__inherit__"


def merge_working_location(current: Any, update: Any) -> dict[str, Any] | None:
    """Reducer for `state["working_location"]`.

    `build_turn_payload` passes `LOCATION_INHERIT` every turn, so the prior
    turn's resolved value (restored by the checkpointer) survives untouched.
    The `resolve_location` node passes a resolved location dict — or `None`
    to clear it — and that replaces the carried value. The sentinel is
    transient: the reducer consumes it, so it never lands in the checkpoint.
    """
    if update == LOCATION_INHERIT:
        return cast("dict[str, Any] | None", current)
    return cast("dict[str, Any] | None", update)


class AgentState(TypedDict):
    """Per-turn state flowing through the LangGraph agent.

    Fields:
      messages            — conversation history; `add_messages` reducer appends.
      taste_profile_summary — behavior-derived preference bullets (per turn).
      memory_summary      — user-stated facts (per turn).
      user_id             — immutable per turn; used as the checkpointer thread_id.
      user_location       — the user's actual location from the request,
                            {lat, lng} or None; set every turn.
      working_location    — the location this turn operates against, a
                            `WorkingLocation.model_dump()` or None; carries
                            across turns via `merge_working_location`.
      location_clarification — reason string when the working location could
                            not be resolved (ambiguous / insufficient); None
                            otherwise. Reset to None each turn.
      movement_profile    — the user's mobility profile from the request
                            (`MovementProfile.model_dump()`) or None when the
                            request omitted it. Plain overwrite, **no
                            reducer**: it is re-supplied verbatim every turn
                            from the request, so — unlike `working_location`,
                            which carries via `merge_working_location` — it
                            must NOT persist. A turn that omits it must see
                            None, not a stale value from an earlier turn.
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
    user_location: dict[str, Any] | None
    working_location: Annotated[dict[str, Any] | None, merge_working_location]
    location_clarification: str | None
    movement_profile: dict[str, Any] | None
    reasoning_steps: list[ReasoningStep]
    # Structured tool-result payloads produced during the current turn.
    # Populated by `finalize_node` from the about-to-be-stripped
    # `ToolMessage`s, so the client can render the consult-family
    # candidates without re-parsing the prose. Reset to [] each turn via
    # `build_turn_payload` (plain overwrite, no reducer).
    tool_results: list[dict[str, Any]]
    # Areas the agent named and `suggest_areas` verified this turn, as
    # `AreaEntity.model_dump()`s in the order the agent named them. The place
    # tools read this to anchor their searches on each area instead of on a
    # disc around the turn's origin (ADR-140) — which is what makes an area an
    # input to retrieval and not only an output of it.
    #
    # An explicit field rather than re-parsing the `suggest_areas` ToolMessage:
    # anchoring is a contract between tools, and a contract that lives in a
    # message another node is free to strip is one that breaks silently. Reset
    # to [] each turn by `build_turn_payload` (plain overwrite, no reducer) —
    # the areas one question was about are not the areas of the next.
    area_anchors: list[dict[str, Any]]
    # Whether the agent said people travel between those areas — set by
    # `suggest_areas(travel_between=...)`. When true the anchors are a path
    # and the stretches between them are searched too; when false they are
    # independent places that happen to be in one answer.
    #
    # The agent's call, never inferred from distance: it knows Hoi An → Hue is
    # a ride people stop along, that two neighborhoods in one city have
    # nothing between them worth pinning, and that Hanoi → Saigon is a flight.
    # Geometry cannot separate those three.
    area_journey: bool
    # The turn's assembled answer (`core/agent/answer.py`) — every tool's
    # candidates as flat items plus the ordered group index, so a client can
    # render Hoi An / the road / Hue without merging tool payloads itself.
    # Populated by `finalize_node`, cleared by `scrub_tool_results_node` for
    # the same reason `tool_results` is: it must never reach the checkpointer.
    answer: dict[str, Any] | None
    steps_taken: int
    error_count: int
    tool_calls_used: int
