"""Tests for the `add_messages_capped` reducer.

Bounds growth of `state["messages"]` in long-lived threads. The cap kicks
in only when the merged list exceeds `state_message_cap`; trimming cuts at
the next HumanMessage boundary at or after `state_message_floor` so
tool_use/tool_result groups stay paired.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from kebi.core.agent.state import add_messages_capped


def _agent_cfg(cap: int, floor: int) -> Any:
    """Build a MagicMock that mirrors AgentConfig for cap/floor only."""
    cfg = MagicMock()
    cfg.return_value.agent.state_message_cap = cap
    cfg.return_value.agent.state_message_floor = floor
    return cfg


def _patch_cfg(cap: int, floor: int) -> Any:
    return patch(
        "kebi.core.config.get_config",
        new=_agent_cfg(cap, floor),
    )


def test_under_cap_is_passthrough() -> None:
    left = [HumanMessage(content=f"m{i}") for i in range(10)]
    right = [AIMessage(content="reply")]
    with _patch_cfg(cap=200, floor=150):
        out = add_messages_capped(left, right)
    assert len(out) == 11
    assert out[-1].content == "reply"


def test_at_cap_is_passthrough() -> None:
    left: list[Any] = [HumanMessage(content=f"m{i}") for i in range(199)]
    right = [AIMessage(content="last")]
    with _patch_cfg(cap=200, floor=150):
        out = add_messages_capped(left, right)
    assert len(out) == 200


def test_over_cap_trims_at_human_boundary() -> None:
    """Merged size 201 > cap 200; trim toward floor=150 at HumanMessage boundary."""
    # Construct turns: [Human, AI, Human, AI, ...] for 100 turns + 1 extra Human.
    msgs: list[Any] = []
    for i in range(100):
        msgs.append(HumanMessage(content=f"h{i}"))
        msgs.append(AIMessage(content=f"a{i}"))
    msgs.append(HumanMessage(content="h100"))  # 201 messages

    with _patch_cfg(cap=200, floor=150):
        out = add_messages_capped([], msgs)

    # Trim window starts at index 201 - 150 = 51 (which is h25), so cut there.
    assert len(out) <= 150
    # First message after trim must be a HumanMessage.
    assert isinstance(out[0], HumanMessage)
    # Last message preserved.
    assert out[-1].content == "h100"


def test_over_cap_does_not_split_tool_call_group() -> None:
    """A trim window that lands inside a tool_use/tool_result pair must skip past it."""
    # Build messages where a tool group sits at the trim boundary.
    # 198 plain Human messages, then AI(tool_call) + ToolMessage + Human (= 201 total).
    msgs: list[Any] = [HumanMessage(content=f"h{i}") for i in range(198)]
    msgs.append(
        AIMessage(content="", tool_calls=[{"name": "recall", "args": {}, "id": "tc1"}])
    )
    msgs.append(ToolMessage(content="result", tool_call_id="tc1"))
    msgs.append(HumanMessage(content="last"))  # 201 messages, last is Human

    with _patch_cfg(cap=200, floor=2):  # tiny floor → window starts near the tail
        out = add_messages_capped([], msgs)

    # The cut must land on a HumanMessage; tool_use/tool_result are not split.
    assert isinstance(out[0], HumanMessage)
    # Verify pairing: no orphaned ToolMessage at the head.
    if isinstance(out[1] if len(out) > 1 else None, ToolMessage):
        # If a ToolMessage is present in the trimmed list, its triggering
        # AIMessage must precede it — otherwise we split a group.
        assert any(
            isinstance(m, AIMessage)
            and any(
                tc.get("id") == out[1].tool_call_id
                for tc in (getattr(m, "tool_calls", None) or [])
            )
            for m in out[:1]
        )


def test_over_cap_with_no_human_in_window_is_noop() -> None:
    """Edge case: trim window has no HumanMessage — leave list untouched."""
    # 201 AIMessages — no HumanMessage anywhere.
    msgs: list[Any] = [AIMessage(content=f"a{i}") for i in range(201)]
    with _patch_cfg(cap=200, floor=150):
        out = add_messages_capped([], msgs)
    # No safe cut point → leave alone.
    assert len(out) == 201


def test_handles_single_message_right_arg() -> None:
    """add_messages accepts a bare BaseMessage as the right arg."""
    left = [HumanMessage(content="hi")]
    with _patch_cfg(cap=200, floor=150):
        out = add_messages_capped(left, AIMessage(content="reply"))
    assert len(out) == 2
    assert out[-1].content == "reply"
