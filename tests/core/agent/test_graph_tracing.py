"""Unit tests for per-attempt span emission in `_invoke_llm_with_retry`.

Direct tests on the helper rather than end-to-end through the graph — the
graph integration is exercised in `test_agent_node.py` and friends; here
we verify the cost-tracking contract: one span per attempt, ERROR on
failed attempts, usage attached on success.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from kebi.core.agent.graph import (
    _ai_message_usage,
    _invoke_llm_with_retry,
    _structured_usage,
)


def test_ai_message_usage_pulls_langchain_metadata():
    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        },
    )
    assert _ai_message_usage(msg) == {"input": 120, "output": 30, "total": 150}


def test_ai_message_usage_returns_empty_dict_when_metadata_missing():
    msg = AIMessage(content="hi")
    assert _ai_message_usage(msg) == {}


def test_structured_usage_unpacks_include_raw_shape():
    raw = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 50,
            "output_tokens": 10,
            "total_tokens": 60,
        },
    )
    result = {"raw": raw, "parsed": MagicMock(), "parsing_error": None}
    assert _structured_usage(result) == {"input": 50, "output": 10, "total": 60}


async def test_invoke_with_retry_emits_one_span_per_attempt_on_first_success():
    spans: list[MagicMock] = []

    def _make_span() -> Any:
        s = MagicMock()
        spans.append(s)
        return s

    bound = MagicMock()

    async def _ainvoke(_conversation: Any) -> AIMessage:
        return AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )

    bound.ainvoke = MagicMock(side_effect=_ainvoke)

    result = await _invoke_llm_with_retry(
        bound,
        [],
        make_span=_make_span,
        extract_usage=_ai_message_usage,
    )

    assert result.content == "ok"
    assert len(spans) == 1
    spans[0].end.assert_called_once()
    end_kwargs = spans[0].end.call_args.kwargs
    assert end_kwargs["usage"] == {"input": 10, "output": 5, "total": 15}
    assert end_kwargs["output"] == {"attempt": 1}


async def test_invoke_with_retry_records_error_then_success_when_first_attempt_fails():
    """One ERROR span followed by one OK span — the cost-attribution contract."""
    spans: list[MagicMock] = []

    def _make_span() -> Any:
        s = MagicMock()
        spans.append(s)
        return s

    call_count = {"n": 0}

    async def _ainvoke(_conversation: Any) -> AIMessage:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")
        return AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )

    bound = MagicMock()
    bound.ainvoke = MagicMock(side_effect=_ainvoke)

    result = await _invoke_llm_with_retry(
        bound,
        [],
        make_span=_make_span,
        extract_usage=_ai_message_usage,
    )

    assert result.content == "ok"
    assert len(spans) == 2
    # First attempt: ERROR
    first_call = spans[0].end.call_args.kwargs
    assert first_call["level"] == "ERROR"
    assert first_call["output"]["attempt"] == 1
    # Second attempt: success
    second_call = spans[1].end.call_args.kwargs
    assert second_call.get("level", "DEFAULT") == "DEFAULT"
    assert second_call["usage"] == {"input": 1, "output": 1, "total": 2}


async def test_invoke_with_retry_raises_after_exhausting_attempts():
    spans: list[MagicMock] = []

    def _make_span() -> Any:
        s = MagicMock()
        spans.append(s)
        return s

    bound = MagicMock()

    async def _ainvoke(_conversation: Any) -> AIMessage:
        raise RuntimeError("always fails")

    bound.ainvoke = MagicMock(side_effect=_ainvoke)

    with pytest.raises(RuntimeError, match="always fails"):
        await _invoke_llm_with_retry(
            bound,
            [],
            make_span=_make_span,
            extract_usage=_ai_message_usage,
        )

    # Every attempt produces an ERROR span — never silently swallowed.
    assert len(spans) == 3
    for span in spans:
        assert span.end.call_args.kwargs["level"] == "ERROR"
