"""Tests for the `finalize` node — strips tool messages before checkpoint."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from kebi.core.agent.graph import (
    NODE_FINALIZE,
    NODE_RESOLVE_LOCATION,
    NODE_SCRUB_TOOL_RESULTS,
    NODE_TOOLS,
    build_graph,
    finalize_node,
    scrub_tool_results_node,
)


def _state(messages: list[Any]) -> dict[str, Any]:
    return {
        "messages": messages,
        "user_id": "user-1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": None,
        "location_clarification": None,
        "movement_profile": None,
        "trip_movement": None,
        "user_profile": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


def test_no_tool_messages_returns_empty_update() -> None:
    """Pure conversation turns produce no RemoveMessage entries."""
    messages = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(content="hello!", id="a1"),
    ]
    update = finalize_node(_state(messages))
    assert update == {}


def test_strips_tool_message() -> None:
    messages = [
        HumanMessage(content="find sushi", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"name": "find_saved", "args": {}, "id": "tc1"}],
        ),
        ToolMessage(content='{"candidates": []}', tool_call_id="tc1", id="t1"),
        AIMessage(content="no luck — try later", id="a2"),
    ]
    update = finalize_node(_state(messages))
    removed = [m.id for m in update["messages"] if isinstance(m, RemoveMessage)]
    # Both the tool-only AIMessage and the ToolMessage get stripped;
    # human + final prose AIMessage survive.
    assert set(removed) == {"a1", "t1"}


def test_keeps_ai_message_with_mixed_text_and_tool_calls() -> None:
    """An AIMessage that has BOTH text and tool_calls is kept (rare but possible)."""
    messages = [
        HumanMessage(content="hi", id="h1"),
        AIMessage(
            content="let me check",
            id="a1",
            tool_calls=[{"name": "find_saved", "args": {}, "id": "tc1"}],
        ),
        ToolMessage(content='{"x": 1}', tool_call_id="tc1", id="t1"),
        AIMessage(content="found something", id="a2"),
    ]
    update = finalize_node(_state(messages))
    removed = {m.id for m in update["messages"] if isinstance(m, RemoveMessage)}
    # Only the ToolMessage is stripped — the tool-call AIMessage carried text.
    assert removed == {"t1"}


def test_skips_messages_without_ids() -> None:
    """RemoveMessage needs an id; messages without one cannot be stripped.

    Tool result capture is independent of `id` — payloads are still
    surfaced into `tool_results` so the response layer can render
    them even when the underlying ToolMessage lacks an id. The strip
    half (`messages` key in the update) is what skips here.
    """
    messages = [
        HumanMessage(content="hi"),  # no id
        ToolMessage(content="{}", tool_call_id="tc1"),  # no id
    ]
    update = finalize_node(_state(messages))
    assert "messages" not in update
    assert update["tool_results"] == [
        {"tool": None, "tool_call_id": "tc1", "payload": {}},
    ]


def test_multiple_tool_calls_in_one_turn() -> None:
    """Multi-step tool invocations all get stripped at finalize time."""
    messages = [
        HumanMessage(content="multi", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"name": "find_saved", "args": {}, "id": "tc1"}],
        ),
        ToolMessage(content='{"a": 1}', tool_call_id="tc1", id="t1"),
        AIMessage(
            content="",
            id="a2",
            tool_calls=[{"name": "find_saved", "args": {}, "id": "tc2"}],
        ),
        ToolMessage(content='{"b": 2}', tool_call_id="tc2", id="t2"),
        AIMessage(content="done", id="a3"),
    ]
    update = finalize_node(_state(messages))
    removed = {m.id for m in update["messages"] if isinstance(m, RemoveMessage)}
    assert removed == {"a1", "t1", "a2", "t2"}


# ---------------------------------------------------------------------------
# Graph-level wiring
# ---------------------------------------------------------------------------


def test_graph_includes_finalize_node(
    mock_llm: MagicMock,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    checkpointer = InMemorySaver()
    app = build_graph(
        mock_llm, [], checkpointer, mock_resolver_llm, mock_geocoding_client
    )
    graph_repr = app.get_graph()
    assert NODE_FINALIZE in graph_repr.nodes


@pytest.mark.asyncio
async def test_finalize_runs_and_strips_in_compiled_graph(
    mock_llm: MagicMock,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    """End-to-end: invoke the compiled graph, confirm final state has no
    ToolMessage and no tool-only AIMessage."""
    # mock_llm returns a plain-text AIMessage by default — no tool call,
    # so the graph routes agent → finalize → END without touching tools.
    checkpointer = InMemorySaver()
    app = build_graph(
        mock_llm, [], checkpointer, mock_resolver_llm, mock_geocoding_client
    )
    config = {"configurable": {"thread_id": "thread-1"}}
    final = await app.ainvoke(
        {
            "messages": [HumanMessage(content="hi", id="h1")],
            "user_id": "user-1",
            "taste_profile_summary": "",
            "memory_summary": "",
            "user_location": None,
            "working_location": None,
            "location_clarification": None,
            "movement_profile": None,
            "trip_movement": None,
            "user_profile": None,
            "reasoning_steps": [],
            "steps_taken": 0,
            "error_count": 0,
            "tool_calls_used": 0,
        },
        config=config,
    )
    msgs = final["messages"]
    assert not any(isinstance(m, ToolMessage) for m in msgs)
    # No tool-only AIMessages either.
    for m in msgs:
        if isinstance(m, AIMessage):
            tcs = getattr(m, "tool_calls", None) or []
            text = (
                m.content
                if isinstance(m.content, str)
                else "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in m.content
                )
            ).strip()
            if tcs:
                assert text, "tool-only AIMessage should have been stripped"


def test_graph_unused_imports_check() -> None:
    """Import sanity — keeps the imports above non-dormant."""
    assert NODE_RESOLVE_LOCATION
    assert NODE_TOOLS
    assert NODE_SCRUB_TOOL_RESULTS


# ---------------------------------------------------------------------------
# tool_results capture + scrub
# ---------------------------------------------------------------------------


def test_finalize_captures_tool_result_payloads() -> None:
    """ToolMessage JSON is parsed into the `tool_results` update key."""
    payload_json = (
        '{"candidates": [{"place": {"place_name": "Gaa"},'
        ' "source": "suggested", "reason": "veg", "rrf_score": 0.0}],'
        ' "empty_reason": null}'
    )
    messages = [
        HumanMessage(content="anything good?", id="h1"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "suggest_places", "args": {}}],
            id="a1",
        ),
        ToolMessage(
            content=payload_json, tool_call_id="tc1", name="suggest_places", id="t1"
        ),
        AIMessage(content="here you go", id="a2"),
    ]
    update = finalize_node(_state(messages))
    assert update["tool_results"] == [
        {
            "tool": "suggest_places",
            "tool_call_id": "tc1",
            "payload": {
                "candidates": [
                    {
                        "place": {"place_name": "Gaa"},
                        "source": "suggested",
                        "reason": "veg",
                        "rrf_score": 0.0,
                    }
                ],
                "empty_reason": None,
            },
        }
    ]
    # And the ToolMessage was still scheduled for removal so it doesn't
    # land in the checkpointer.
    removed = {m.id for m in update["messages"] if isinstance(m, RemoveMessage)}
    assert "t1" in removed


def test_finalize_captures_payload_for_each_tool_call() -> None:
    """Both-tools turns produce one tool_results entry per tool call."""
    messages = [
        HumanMessage(content="open ended", id="h1"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "tc1", "name": "find_saved", "args": {}},
                {"id": "tc2", "name": "suggest_places", "args": {}},
            ],
            id="a1",
        ),
        ToolMessage(
            content='{"candidates": [], "empty_reason": "no_saves"}',
            tool_call_id="tc1",
            name="find_saved",
            id="t1",
        ),
        ToolMessage(
            content='{"candidates": [], "empty_reason": null}',
            tool_call_id="tc2",
            name="suggest_places",
            id="t2",
        ),
        AIMessage(content="prose answer", id="a2"),
    ]
    update = finalize_node(_state(messages))
    tools = [r["tool"] for r in update["tool_results"]]
    assert tools == ["find_saved", "suggest_places"]


def test_finalize_handles_tool_message_error_status() -> None:
    """LangGraph's tool-validation error path yields a structured payload."""
    err_msg = ToolMessage(
        content="missing arg foo",
        tool_call_id="tc1",
        name="suggest_places",
        id="t1",
        status="error",
    )
    update = finalize_node(_state([HumanMessage(content="hi", id="h1"), err_msg]))
    assert update["tool_results"][0]["payload"] == {
        "error": "tool_call_failed",
        "message": "missing arg foo",
    }


def test_scrub_tool_results_node_clears_field() -> None:
    """Both payload channels are emptied to keep the checkpoint clean.

    `tool_payloads` carries the untrimmed results server-side (ADR-139), so it
    would bloat the per-thread checkpointer exactly like `tool_results`.
    """
    state = _state([])
    state["tool_results"] = [{"tool": "x", "tool_call_id": "y", "payload": {}}]
    state["tool_payloads"] = [{"tool": "x", "tool_call_id": "y", "payload": {}}]
    assert scrub_tool_results_node(state) == {
        "tool_results": [],
        "tool_payloads": [],
    }
