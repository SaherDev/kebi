"""Unit tests for the SSE step-lifecycle emitter (ADR-102)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done


def _capture() -> tuple[list[dict[str, Any]], Any]:
    """Return a (sink, writer) pair where writer appends each frame to sink."""
    sink: list[dict[str, Any]] = []
    return sink, sink.append


def test_emit_step_active_builds_active_frame() -> None:
    sink, writer = _capture()
    with patch("kebi.core.agent.stream_emit.get_stream_writer", return_value=writer):
        emit_step_active("find_saved#0", "find_saved", source="agent")
    assert len(sink) == 1
    frame = sink[0]
    assert frame["id"] == "find_saved#0"
    assert frame["step"] == "find_saved"
    assert frame["status"] == "active"
    assert frame["summary"] is None
    assert frame["duration_ms"] is None
    assert frame["source"] == "agent"
    assert frame["visibility"] == "user"


def test_emit_step_done_decorates_the_real_step() -> None:
    sink, writer = _capture()
    step = ReasoningStep(
        step="find_saved.summary",
        summary="Found 2 saved spots — A, B.",
        source="agent",
        visibility="user",
        duration_ms=420.0,
    )
    with patch("kebi.core.agent.stream_emit.get_stream_writer", return_value=writer):
        emit_step_done("find_saved#0", step)
    assert len(sink) == 1
    frame = sink[0]
    assert frame["id"] == "find_saved#0"
    assert frame["step"] == "find_saved.summary"
    assert frame["status"] == "done"
    assert frame["summary"] == "Found 2 saved spots — A, B."
    assert frame["duration_ms"] == 420.0


def test_done_stamps_measured_duration_on_step_and_frame() -> None:
    """An active→done pair stamps a real duration on the step and the frame."""
    sink, writer = _capture()
    step = ReasoningStep(
        step="find_saved.summary",
        summary="Found 1 saved spot — A.",
        source="agent",
        duration_ms=0.0,
    )
    with patch("kebi.core.agent.stream_emit.get_stream_writer", return_value=writer):
        started = emit_step_active("find_saved#0", "find_saved", source="agent")
        emit_step_done("find_saved#0", step, started=started)

    active, done = sink
    assert active["status"] == "active"
    assert active["duration_ms"] is None
    assert done["status"] == "done"
    # Measured (non-placeholder) duration, mirrored onto the persisted step.
    assert done["duration_ms"] is not None
    assert done["duration_ms"] >= 0.0
    assert step.duration_ms == done["duration_ms"]


def test_done_without_active_keeps_constructed_duration() -> None:
    """No matching active → the step's own duration is left untouched."""
    sink, writer = _capture()
    step = ReasoningStep(step="x", summary="y", source="agent", duration_ms=123.0)
    with patch("kebi.core.agent.stream_emit.get_stream_writer", return_value=writer):
        emit_step_done("orphan#0", step)
    assert step.duration_ms == 123.0
    assert sink[0]["duration_ms"] == 123.0


def test_emit_preserves_debug_visibility() -> None:
    sink, writer = _capture()
    with patch("kebi.core.agent.stream_emit.get_stream_writer", return_value=writer):
        emit_step_active("x#0", "x", source="fallback", visibility="debug")
    assert sink[0]["visibility"] == "debug"
    assert sink[0]["source"] == "fallback"


def test_emit_is_noop_without_a_writer() -> None:
    """Off the stream (JSON path) get_stream_writer raises — emit must no-op."""

    def _raise() -> Any:
        raise RuntimeError("not in a streaming run")

    step = ReasoningStep(step="s", summary="x", source="agent")
    with patch("kebi.core.agent.stream_emit.get_stream_writer", side_effect=_raise):
        # Neither call should raise.
        emit_step_active("s#0", "s", source="agent")
        emit_step_done("s#0", step)


def test_persisted_step_carries_no_lifecycle_markers() -> None:
    """A step built the normal way (not via emit) has id/status = None.

    Guards the invariant that the non-stream JSON path returns steps with no
    lifecycle tagging — the markers are set only as frames go out.
    """
    step = ReasoningStep(step="s", summary="x", source="agent")
    assert step.id is None
    assert step.status is None
