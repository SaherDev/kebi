"""Unit tests for `core/agent/_trace_context.py`.

Covers `current_tool` ContextVar isolation across concurrent asyncio tasks
(the regression guard called out in the plan), the `set_tool` reset
contract, and the `feature_span` helper picking up the ContextVar.
"""

from __future__ import annotations

import asyncio

from kebi.core.agent._trace_context import (
    current_tool,
    feature_span,
    feature_trace,
    set_tool,
)


def test_set_tool_resets_on_exit():
    assert current_tool.get() is None
    with set_tool("find_saved"):
        assert current_tool.get() == "find_saved"
    assert current_tool.get() is None


def test_set_tool_nested():
    with set_tool("outer"):
        assert current_tool.get() == "outer"
        with set_tool("inner"):
            assert current_tool.get() == "inner"
        assert current_tool.get() == "outer"
    assert current_tool.get() is None


async def test_concurrent_tasks_see_their_own_tool():
    """Two tasks set different tools in parallel; neither pollutes the other."""
    observed: dict[str, str | None] = {}

    async def _task(name: str) -> None:
        with set_tool(name):
            # yield control so the other task gets to run inside its own
            # set_tool block — the ContextVar should remain task-local.
            await asyncio.sleep(0)
            observed[name] = current_tool.get()

    await asyncio.gather(_task("find_saved"), _task("suggest_places"))
    assert observed == {
        "find_saved": "find_saved",
        "suggest_places": "suggest_places",
    }


def test_feature_span_reads_current_tool_when_not_passed_explicit(monkeypatch):
    """`feature_span` reads `current_tool` so nested services inherit the tool."""
    from kebi.providers import tracing

    captured: dict[str, object] = {}

    class _FakeTracer:
        def generation(self, **kwargs):
            captured.update(kwargs)
            return tracing._NullSpan()

    monkeypatch.setattr(tracing, "get_tracing_client", lambda: _FakeTracer())
    monkeypatch.setattr(
        "kebi.core.agent._trace_context.get_tracing_client", lambda: _FakeTracer()
    )

    with set_tool("suggest_places"):
        feature_span("candidate_namer", "agent_tool", user_id="u1", model="m")

    assert captured["metadata"] == {
        "feature": "agent_tool",
        "tool": "suggest_places",
    }
    assert captured["user_id"] == "u1"
    assert captured["model"] == "m"
    assert captured["name"] == "candidate_namer"


async def test_feature_trace_uses_tracing_client_trace_with_tags(monkeypatch):
    """`feature_trace` opens a trace tagged with the feature."""
    from contextlib import asynccontextmanager

    from kebi.providers import tracing

    captured: dict[str, object] = {}

    class _FakeTracer:
        @asynccontextmanager
        async def trace(
            self,
            name,
            user_id=None,
            session_id=None,
            metadata=None,
            tags=None,
        ):
            captured["name"] = name
            captured["user_id"] = user_id
            captured["session_id"] = session_id
            captured["metadata"] = metadata
            captured["tags"] = tags
            yield

    monkeypatch.setattr(tracing, "get_tracing_client", lambda: _FakeTracer())
    monkeypatch.setattr(
        "kebi.core.agent._trace_context.get_tracing_client", lambda: _FakeTracer()
    )

    async with feature_trace("chat", "u1", name="chat_turn", extra={"k": "v"}):
        pass

    assert captured["name"] == "chat_turn"
    assert captured["user_id"] == "u1"
    assert captured["session_id"] == "u1"
    assert captured["tags"] == ["feature:chat"]
    assert captured["metadata"] == {"feature": "chat", "k": "v"}
