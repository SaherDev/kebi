"""Tests for build_emit_closure + append_summary (feature 028 M5)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

from kebi.core.agent.tools._emit import append_summary, build_emit_closure


def test_build_emit_closure_returns_collected_and_emit() -> None:
    collected, emit = build_emit_closure("recall")
    assert collected == []
    assert callable(emit)


def test_emit_appends_debug_step_with_auto_duration() -> None:
    collected, emit = build_emit_closure("recall")
    time.sleep(0.005)
    emit("recall.mode", "mode=hybrid")

    assert len(collected) == 1
    step = collected[0]
    assert step.step == "recall.mode"
    assert step.summary == "mode=hybrid"
    assert step.source == "tool"
    assert step.tool_name == "recall"
    assert step.visibility == "debug"
    assert step.duration_ms is not None
    assert step.duration_ms >= 0.0


def test_emit_uses_explicit_duration_verbatim() -> None:
    collected, emit = build_emit_closure("recall")
    emit("recall.enrich", "2 candidates", duration_ms=42.0)

    assert collected[0].duration_ms == 42.0


def test_append_summary_adds_user_visible_tool_summary() -> None:
    collected, emit = build_emit_closure("consult")
    emit("consult.discover", "5 candidates")
    append_summary(collected, "consult", "Ranked 5 nearby options")

    summary_step = collected[-1]
    assert summary_step.step == "tool.summary"
    assert summary_step.summary == "Ranked 5 nearby options"
    assert summary_step.source == "tool"
    assert summary_step.tool_name == "consult"
    assert summary_step.visibility == "user"
    assert summary_step.duration_ms is not None


def test_append_summary_duration_is_total_elapsed_from_first_emit() -> None:
    collected, emit = build_emit_closure("recall")
    emit("recall.mode", "mode=filter")
    time.sleep(0.01)
    append_summary(collected, "recall", "found 3 places")

    # The append_summary duration_ms should reflect ~10ms elapsed from first emit.
    summary_step = collected[-1]
    assert summary_step.duration_ms is not None
    assert summary_step.duration_ms >= 5.0


def test_append_summary_zero_duration_when_no_prior_emits() -> None:
    collected, _emit = build_emit_closure("recall")
    append_summary(collected, "recall", "zero-work summary")
    assert collected[0].duration_ms == 0.0


def test_stream_writer_fan_out_fires_when_attached() -> None:
    writes: list[Any] = []

    def fake_writer(payload: Any) -> None:
        writes.append(payload)

    with patch(
        "kebi.core.agent.tools._emit._get_writer_safe",
        return_value=fake_writer,
    ):
        collected, emit = build_emit_closure("recall")
        emit("recall.mode", "mode=hybrid")
        append_summary(collected, "recall", "done")

    steps_written = [w.get("step") for w in writes]
    assert "recall.mode" in steps_written
    assert "tool.summary" in steps_written


def test_stream_writer_none_is_silent_noop() -> None:
    # Default behavior outside a runnable context: _get_writer_safe returns None.
    collected, emit = build_emit_closure("consult")
    emit("consult.parse_input", "no input")
    append_summary(collected, "consult", "done")

    # Just verify nothing crashes and the collected list is still complete.
    assert len(collected) == 2
