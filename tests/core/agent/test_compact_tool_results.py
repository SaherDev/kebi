"""Tests for ToolMessage compaction helpers in graph.py.

Older tool payloads (recall/consult/save JSON) get replaced by a one-line
breadcrumb on the way to the LLM. The most recent N stay full. State is
untouched — only the per-call conversation list is rewritten.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from kebi.core.agent.graph import (
    _compact_old_tool_results,
    _summarize_tool_payload,
)


def _tool_msg(name: str, payload: dict | str, tcid: str) -> ToolMessage:
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return ToolMessage(content=content, tool_call_id=tcid, name=name)


def test_summarize_results_payload_with_names() -> None:
    msg = _tool_msg(
        "recall",
        {
            "results": [
                {"place": {"place_name": "Cafe Noir"}},
                {"place": {"place_name": "Bun Bo Hue"}},
                {"place": {"place_name": "The Citadel"}},
            ]
        },
        "tc1",
    )
    out = _summarize_tool_payload(msg)
    assert out == (
        "[recall] earlier call returned 3 result(s): "
        "Cafe Noir, Bun Bo Hue, The Citadel"
    )


def test_summarize_results_caps_at_six_names() -> None:
    results = [{"place": {"place_name": f"P{i}"}} for i in range(10)]
    msg = _tool_msg("consult", {"results": results}, "tc1")
    out = _summarize_tool_payload(msg)
    assert "P0, P1, P2, P3, P4, P5" in out
    assert "(+4 more)" in out
    # Names past the cap should NOT appear.
    assert "P6" not in out


def test_summarize_results_without_names_falls_back_to_count() -> None:
    msg = _tool_msg("recall", {"results": [{"x": 1}, {"x": 2}]}, "tc1")
    out = _summarize_tool_payload(msg)
    assert out == "[recall] earlier call returned 2 result(s); details elided"


def test_summarize_save_results_use_top_level_place_name() -> None:
    """Save results may use top-level place_name (not nested under .place)."""
    msg = _tool_msg(
        "save",
        {"results": [{"status": "saved", "place_name": "Vondelpark"}]},
        "tc1",
    )
    out = _summarize_tool_payload(msg)
    assert "Vondelpark" in out


def test_summarize_status_payload() -> None:
    msg = _tool_msg("save", {"status": "saved", "request_id": "r1"}, "tc1")
    assert "status=saved" in _summarize_tool_payload(msg)


def test_summarize_error_payload() -> None:
    msg = _tool_msg(
        "consult",
        {"error": "tool_invocation_failed", "type": "ValidationError"},
        "tc1",
    )
    out = _summarize_tool_payload(msg)
    assert "errored" in out
    assert "ValidationError" in out


def test_summarize_malformed_json() -> None:
    msg = _tool_msg("recall", "not-json-at-all", "tc1")
    out = _summarize_tool_payload(msg)
    assert "elided" in out
    assert "15 chars" in out


def test_compact_noop_when_under_window() -> None:
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content="ok"),
        _tool_msg("recall", {"results": [{}]}, "tc1"),
    ]
    out, n = _compact_old_tool_results(msgs, keep_recent=2)
    assert n == 0
    assert out == msgs


def test_compact_keeps_recent_replaces_older() -> None:
    big_payload = {
        "results": [{"place": {"place_name": f"P{i}"}} for i in range(10)]
    }
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[{"name": "recall", "args": {}, "id": "tc1"}]),
        _tool_msg("recall", big_payload, "tc1"),
        AIMessage(content="", tool_calls=[{"name": "recall", "args": {}, "id": "tc2"}]),
        _tool_msg("recall", big_payload, "tc2"),
        AIMessage(content="", tool_calls=[{"name": "recall", "args": {}, "id": "tc3"}]),
        _tool_msg("recall", big_payload, "tc3"),
    ]
    out, n = _compact_old_tool_results(msgs, keep_recent=1)
    assert n == 2

    # First two ToolMessages should be compacted; last one untouched.
    tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 3
    assert tool_msgs[0].tool_call_id == "tc1"
    assert tool_msgs[1].tool_call_id == "tc2"
    assert tool_msgs[2].tool_call_id == "tc3"
    # Compacted entries keep place names so the LLM can still reference them.
    assert "P0" in str(tool_msgs[0].content)
    assert "returned 10 result(s)" in str(tool_msgs[1].content)
    # Last one keeps its full JSON.
    assert json.loads(str(tool_msgs[2].content)) == big_payload


def test_compact_keep_recent_zero_compacts_all() -> None:
    msgs = [
        _tool_msg("recall", {"results": [{}]}, "tc1"),
        _tool_msg("consult", {"results": [{}, {}]}, "tc2"),
    ]
    out, n = _compact_old_tool_results(msgs, keep_recent=0)
    assert n == 2
    for m in out:
        assert isinstance(m, ToolMessage)
        body = str(m.content)
        # Compact form is much shorter than the original JSON.
        assert "earlier call" in body


def test_compact_negative_window_is_noop() -> None:
    msgs = [_tool_msg("recall", {"results": [{}]}, "tc1")]
    out, n = _compact_old_tool_results(msgs, keep_recent=-1)
    assert n == 0
    assert out == msgs


def test_compact_does_not_mutate_input_messages() -> None:
    original = _tool_msg("recall", {"results": [{"name": "p1"}, {"name": "p2"}]}, "tc1")
    original_content = original.content
    msgs = [original, _tool_msg("recall", {"results": [{}]}, "tc2")]
    _compact_old_tool_results(msgs, keep_recent=1)
    # Original ToolMessage instance must keep its full payload.
    assert original.content == original_content
