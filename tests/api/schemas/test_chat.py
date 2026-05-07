"""Tests for ChatResponse schema (ADR-065 updated Literal)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.api.schemas.chat import ChatResponse


def test_chat_response_accepts_all_valid_types() -> None:
    """All valid ChatResponse types are accepted by the schema (ADR-065)."""
    valid = (
        "extract-place",
        "consult",
        "recall",
        "agent",
        "clarification",
        "error",
    )
    for t in valid:
        resp = ChatResponse(type=t, message="m")  # type: ignore[arg-type]
        assert resp.type == t


def test_chat_response_rejects_legacy_assistant_type() -> None:
    """'assistant' was removed in ADR-065 and must not be accepted."""
    with pytest.raises(ValidationError):
        ChatResponse(type="assistant", message="m")  # type: ignore[arg-type]


def test_chat_response_accepts_agent_type_value() -> None:
    resp = ChatResponse(type="agent", message="hello", data={"reasoning_steps": []})
    assert resp.type == "agent"
    assert resp.data == {"reasoning_steps": []}


def test_chat_response_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ChatResponse(type="nonsense", message="m")  # type: ignore[arg-type]
