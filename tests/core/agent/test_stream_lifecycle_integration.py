"""Integration: the compiled graph emits active→done frames over astream (ADR-102).

Unlike the unit tests for `stream_emit` (which patch the writer) and the route
tests (which mock the graph), this drives the REAL compiled graph through
`astream(stream_mode=["custom", "values"])` with a fake LLM, so it verifies the
node→`get_stream_writer()`→custom-stream path actually carries frames.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from kebi.core.agent.graph import build_graph
from kebi.core.agent.invocation import build_turn_payload


async def _custom_frames(graph: Any, message: str) -> list[dict[str, Any]]:
    payload = build_turn_payload(
        message=message,
        user_id="u1",
        taste_profile_summary="",
        memory_summary="",
    )
    config = {"configurable": {"thread_id": "u1"}}
    frames: list[dict[str, Any]] = []
    async for mode, chunk in graph.astream(
        payload, config=config, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            frames.append(chunk)
    return frames


async def test_agent_decision_streams_active_then_done(
    mock_llm: MagicMock,
    no_tools: list[Any],
    checkpointer: InMemorySaver,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    """A direct-answer turn emits one active then one done agent frame."""

    async def _answer(_messages: Any) -> AIMessage:
        return AIMessage(content="here is my answer")

    mock_llm.ainvoke = MagicMock(side_effect=_answer)
    graph = build_graph(
        mock_llm, no_tools, checkpointer, mock_resolver_llm, mock_geocoding_client
    )

    # "hi there" trips no location gate → straight to the agent node, so the
    # only frames are the agent.tool_decision lifecycle pair.
    frames = await _custom_frames(graph, "hi there")
    decision = [f for f in frames if str(f["id"]).startswith("agent.tool_decision")]

    assert [f["status"] for f in decision] == ["active", "done"]
    assert decision[0]["id"] == decision[1]["id"]
    assert decision[0]["summary"] is None
    assert decision[0]["duration_ms"] is None
    # done carries the full LLM text and a real measured duration (not the
    # 0.0 placeholder the step is constructed with).
    assert decision[1]["summary"] == "here is my answer"
    assert decision[1]["duration_ms"] is not None
    assert decision[1]["duration_ms"] >= 0.0


async def test_every_done_frame_has_a_prior_active(
    mock_llm: MagicMock,
    no_tools: list[Any],
    checkpointer: InMemorySaver,
    mock_resolver_llm: MagicMock,
    mock_geocoding_client: MagicMock,
) -> None:
    """Lifecycle contract holds end-to-end across whatever the turn emits."""

    async def _answer(_messages: Any) -> AIMessage:
        return AIMessage(content="ok")

    mock_llm.ainvoke = MagicMock(side_effect=_answer)
    graph = build_graph(
        mock_llm, no_tools, checkpointer, mock_resolver_llm, mock_geocoding_client
    )

    frames = await _custom_frames(graph, "hi there")

    seen_active: set[str] = set()
    for frame in frames:
        if frame["status"] == "active":
            seen_active.add(frame["id"])
        else:  # done
            assert frame["id"] in seen_active, (
                f"done frame {frame['id']} had no prior active frame"
            )
