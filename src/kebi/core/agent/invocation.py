"""Per-turn AgentState payload builder (feature 027 M3, FR-022).

Single construction site for per-turn state updates. Resets the transient
`reasoning_steps` field so it cannot drift across turns. Any future
invocation site (streaming endpoint, retry path) must route through this
helper.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from kebi.core.agent.state import LOCATION_INHERIT


def build_turn_payload(
    message: str,
    user_id: str,
    taste_profile_summary: str,
    memory_summary: str,
    user_location: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-turn state update for `graph.ainvoke(...)`.

    LangGraph's default state-merge semantics overwrite non-reducer fields
    with whatever the incoming payload contains. For `messages` (reducer:
    add_messages), a single-element list appends to history. For
    `reasoning_steps` (no reducer), passing `[]` resets it.

    `working_location` is passed the `LOCATION_INHERIT` sentinel, not a real
    value. Its reducer (`merge_working_location`) maps the sentinel to "keep
    the prior turn's value", so the location resolved last turn carries
    forward unless the `resolve_location` node replaces it. This is the
    explicit carry-forward contract — do not "complete" this dict with a
    concrete `working_location`, it would clobber the carried value.

    Args:
      message: user-supplied input for this turn.
      user_id: checkpointer thread_id; identifies the conversation.
      taste_profile_summary: behavior-derived preference bullets.
      memory_summary: user-stated facts with confidence scores.
      user_location: the user's actual location, optional {lat, lng}.

    Returns:
      dict payload suitable for `graph.ainvoke(payload, config=...)`.
    """
    return {
        "messages": [HumanMessage(content=message)],
        "reasoning_steps": [],
        "taste_profile_summary": taste_profile_summary,
        "memory_summary": memory_summary,
        "user_id": user_id,
        "user_location": user_location,
        "working_location": LOCATION_INHERIT,
        "location_clarification": None,
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }
