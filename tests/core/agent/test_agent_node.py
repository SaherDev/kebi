"""Tests for agent_node (feature 027 M3, FR-028).

M3 scope: structural only — node binds LLM, renders prompt, appends
response, increments steps_taken. Real LLM wiring lands in M6
(clarification 2026-04-21); tests here use a mocked LLM.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from kebi.core.agent.graph import make_agent_node


def _system_text(msg: SystemMessage) -> str:
    """Extract the text from a SystemMessage regardless of caching format."""
    if isinstance(msg.content, str):
        return msg.content
    return "".join(
        block["text"] for block in msg.content if block.get("type") == "text"
    )


def _base_state(**overrides: object) -> dict:
    state = {
        "messages": [HumanMessage(content="find me ramen")],
        "taste_profile_summary": "TASTE-SUBSTITUTED",
        "memory_summary": "MEMORY-SUBSTITUTED",
        "user_id": "u1",
        "location": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
    }
    state.update(overrides)  # type: ignore[arg-type]
    return state


@pytest.fixture
def captured_llm() -> MagicMock:
    """Mock LLM that records every call to ainvoke and returns a fixed AIMessage."""
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=llm)

    async def _ainvoke(messages: Any) -> AIMessage:
        return AIMessage(content="mocked agent response")

    llm.ainvoke = MagicMock(side_effect=_ainvoke)
    return llm


async def test_agent_node_binds_tools_via_injected_llm(
    captured_llm: MagicMock,
) -> None:
    tools: list[Any] = []
    make_agent_node(captured_llm, tools)
    captured_llm.bind_tools.assert_called_once_with(tools, parallel_tool_calls=False)


async def test_agent_node_renders_prompt_with_both_slots_substituted(
    captured_llm: MagicMock,
) -> None:
    node = make_agent_node(captured_llm, [])
    await node(_base_state())

    captured_llm.ainvoke.assert_called_once()
    sent_messages = captured_llm.ainvoke.call_args.args[0]
    system = sent_messages[0]
    assert isinstance(system, SystemMessage)
    text = _system_text(system)
    # Both template slot literals must be GONE from the rendered prompt.
    assert "{taste_profile_summary}" not in text
    assert "{memory_summary}" not in text
    # And both substitution values must be PRESENT.
    assert "TASTE-SUBSTITUTED" in text
    assert "MEMORY-SUBSTITUTED" in text


async def test_agent_node_caches_static_head_as_separate_block(
    captured_llm: MagicMock,
) -> None:
    """With caching enabled (the default config), the system message splits
    into a cached static head carrying the cache breakpoint and an uncached
    per-turn dynamic tail (ADR-100). The head must be slot-free and identical
    across turns; the per-turn values live only in the tail."""
    node = make_agent_node(captured_llm, [])
    await node(_base_state())

    system = captured_llm.ainvoke.call_args.args[0][0]
    assert isinstance(system, SystemMessage)
    blocks = system.content
    assert isinstance(blocks, list) and len(blocks) == 2
    head, tail = blocks
    assert head["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tail
    # Static head is slot-free and free of per-turn user values; the tail
    # carries the substituted taste/memory.
    assert "{" not in head["text"]
    assert "TASTE-SUBSTITUTED" not in head["text"]
    assert "TASTE-SUBSTITUTED" in tail["text"]
    assert "MEMORY-SUBSTITUTED" in tail["text"]


async def test_agent_node_sends_system_plus_messages(captured_llm: MagicMock) -> None:
    node = make_agent_node(captured_llm, [])
    state = _base_state(messages=[HumanMessage(content="hello")])
    await node(state)

    sent_messages = captured_llm.ainvoke.call_args.args[0]
    assert len(sent_messages) == 2
    assert isinstance(sent_messages[0], SystemMessage)
    assert isinstance(sent_messages[1], HumanMessage)
    assert sent_messages[1].content == "hello"


async def test_agent_node_increments_steps_taken(captured_llm: MagicMock) -> None:
    node = make_agent_node(captured_llm, [])
    update = await node(_base_state(steps_taken=2))
    assert update["steps_taken"] == 3


async def test_agent_node_appends_ai_message(captured_llm: MagicMock) -> None:
    node = make_agent_node(captured_llm, [])
    update = await node(_base_state())
    msgs = update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], AIMessage)
    assert msgs[0].content == "mocked agent response"


async def test_agent_node_empty_summaries_still_renders(
    captured_llm: MagicMock,
) -> None:
    """Slot substitution tolerates empty strings (used by routing/skeleton tests)."""
    node = make_agent_node(captured_llm, [])
    await node(_base_state(taste_profile_summary="", memory_summary=""))
    sent_messages = captured_llm.ainvoke.call_args.args[0]
    system = sent_messages[0]
    text = _system_text(system)
    assert "{taste_profile_summary}" not in text
    assert "{memory_summary}" not in text


async def test_agent_node_trims_history_to_max_history_messages(
    captured_llm: MagicMock,
) -> None:
    """60-turn conversation: only the last max_history_messages are sent to the LLM."""
    node = make_agent_node(captured_llm, [])
    messages = [HumanMessage(content=f"msg {i}") for i in range(60)]
    with patch("kebi.core.agent.graph.get_config") as mock_cfg:
        mock_cfg.return_value.agent.max_history_messages = 40
        mock_cfg.return_value.agent.tool_result_window = 2
        mock_cfg.return_value.prompts = {
            "agent": MagicMock(
                content="prompt {taste_profile_summary} {memory_summary}"
            )
        }
        await node(_base_state(messages=messages))

    sent_messages = captured_llm.ainvoke.call_args.args[0]
    # system prompt is index 0; remaining are trimmed history
    history_sent = sent_messages[1:]
    assert len(history_sent) == 40
    assert history_sent[0].content == "msg 20"
    assert history_sent[-1].content == "msg 59"


async def test_agent_node_trim_before_sanitize_no_orphaned_tool_message(
    captured_llm: MagicMock,
) -> None:
    """Trim must happen before sanitize so a ToolMessage is never at position 0.

    Scenario: history has an AIMessage with a tool_call at position 39 (0-indexed
    from the full list) and its ToolMessage at position 40. After trimming to 40
    messages we keep indices 21-60, so the AIMessage lands at index 0 of the
    trimmed slice — sanitize then sees it has a satisfied ToolMessage following
    it and does NOT inject a placeholder. The LLM receives both messages cleanly.
    """
    tool_call_id = "tc_abc"
    ai_with_tool = AIMessage(
        content="",
        tool_calls=[{"id": tool_call_id, "name": "recall", "args": {}}],
    )
    tool_result = ToolMessage(content="results", tool_call_id=tool_call_id)

    # 39 plain human messages, then the AI+tool pair = 41 total
    messages: list[Any] = [HumanMessage(content=f"msg {i}") for i in range(39)]
    messages.append(ai_with_tool)
    messages.append(tool_result)

    node = make_agent_node(captured_llm, [])
    with patch("kebi.core.agent.graph.get_config") as mock_cfg:
        mock_cfg.return_value.agent.max_history_messages = 40
        mock_cfg.return_value.agent.tool_result_window = 2
        mock_cfg.return_value.prompts = {
            "agent": MagicMock(
                content="prompt {taste_profile_summary} {memory_summary}"
            )
        }
        await node(_base_state(messages=messages))

    sent_messages = captured_llm.ainvoke.call_args.args[0]
    history_sent = sent_messages[1:]
    assert len(history_sent) == 40
    # The AI message with the tool call is present and its ToolMessage follows it —
    # no synthetic placeholder was injected.
    ai_positions = [i for i, m in enumerate(history_sent) if isinstance(m, AIMessage)]
    assert ai_positions, "AIMessage not found in trimmed history"
    ai_idx = ai_positions[-1]
    assert ai_idx + 1 < len(history_sent)
    assert isinstance(history_sent[ai_idx + 1], ToolMessage)
    assert history_sent[ai_idx + 1].tool_call_id == tool_call_id
