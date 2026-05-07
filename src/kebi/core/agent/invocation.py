"""Per-turn AgentState payload builder (feature 027 M3, FR-022).

Single construction site for per-turn state updates. Resets both transient
fields (`last_recall_results`, `reasoning_steps`) in lockstep so they
cannot drift across turns. Any future invocation site (streaming endpoint,
retry path) must route through this helper.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage


def build_turn_payload(
    message: str,
    user_id: str,
    taste_profile_summary: str,
    memory_summary: str,
    location: dict[str, float] | None = None,
    location_label: str | None = None,
) -> dict[str, Any]:
    """Build the per-turn state update for `graph.ainvoke(...)`.

    LangGraph's default state-merge semantics overwrite non-reducer fields
    with whatever the incoming payload contains. For `messages` (reducer:
    add_messages), a single-element list appends to history. For
    `last_recall_results` and `reasoning_steps` (no reducer), passing
    `None` / `[]` resets them.

    Args:
      message: user-supplied input for this turn.
      user_id: checkpointer thread_id; identifies the conversation.
      taste_profile_summary: behavior-derived preference bullets.
      memory_summary: user-stated facts with confidence scores.
      location: optional {lat, lng} context.
      location_label: optional "City, Country" label resolved server-side
        from `location`. Gives the agent a city name to reason about —
        coords alone are too low-info for the LLM.

    Returns:
      dict payload suitable for `graph.ainvoke(payload, config=...)`.
    """
    return {
        "messages": [HumanMessage(content=message)],
        "last_recall_results": None,
        "reasoning_steps": [],
        "taste_profile_summary": taste_profile_summary,
        "memory_summary": memory_summary,
        "user_id": user_id,
        "location": location,
        "location_label": location_label,
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }
