"""Tests for ChatRequest / ChatResponse schemas (ADR-065, ADR-084, ADR-085)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.api.schemas.chat import ChatRequest, ChatResponse, MovementProfile


def test_chat_response_accepts_all_valid_types() -> None:
    """ADR-075 narrowed ChatResponseType to agent|error (recall/consult
    tools removed — the agent is a zero-tool Q&A surface)."""
    valid = ("agent", "error")
    for t in valid:
        resp = ChatResponse(type=t, message="m")  # type: ignore[arg-type]
        assert resp.type == t


def test_chat_response_rejects_removed_consult_recall_types() -> None:
    """'consult' and 'recall' were removed by ADR-075."""
    for t in ("consult", "recall"):
        with pytest.raises(ValidationError):
            ChatResponse(type=t, message="m")  # type: ignore[arg-type]


def test_chat_response_rejects_legacy_assistant_type() -> None:
    """'assistant' was removed in ADR-065 and must not be accepted."""
    with pytest.raises(ValidationError):
        ChatResponse(type="assistant", message="m")  # type: ignore[arg-type]


def test_chat_response_rejects_legacy_extract_place_type() -> None:
    """'extract-place' was removed by ADR-073 — saves no longer flow through chat."""
    with pytest.raises(ValidationError):
        ChatResponse(type="extract-place", message="m")  # type: ignore[arg-type]


def test_chat_response_rejects_legacy_clarification_type() -> None:
    """'clarification' was removed by ADR-073 — no GraphInterrupt producer remains."""
    with pytest.raises(ValidationError):
        ChatResponse(type="clarification", message="m")  # type: ignore[arg-type]


def test_chat_response_accepts_agent_type_value() -> None:
    resp = ChatResponse(type="agent", message="hello", data={"reasoning_steps": []})
    assert resp.type == "agent"
    assert resp.data == {"reasoning_steps": []}


def test_chat_response_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ChatResponse(type="nonsense", message="m")  # type: ignore[arg-type]


# --- ChatRequest.movement_profile (ADR-084 / ADR-085) ----------------------


def test_chat_request_parses_valid_movement_profile() -> None:
    req = ChatRequest.model_validate(
        {
            "user_id": "u1",
            "message": "dinner nearby",
            "movement_profile": {
                "available_modes": ["walking", "transit"],
                "reach": "far",
            },
        }
    )
    assert req.movement_profile is not None
    assert req.movement_profile.available_modes == ["walking", "transit"]
    assert req.movement_profile.reach == "far"


def test_chat_request_movement_profile_defaults_to_none() -> None:
    req = ChatRequest(user_id="u1", message="hi")
    assert req.movement_profile is None


def test_movement_profile_reach_defaults_to_normal() -> None:
    profile = MovementProfile(available_modes=["walking"])
    assert profile.reach == "normal"


def test_movement_profile_rejects_invalid_mode() -> None:
    with pytest.raises(ValidationError):
        MovementProfile(
            available_modes=["teleport"],  # type: ignore[list-item]
        )


def test_movement_profile_rejects_empty_available_modes() -> None:
    with pytest.raises(ValidationError):
        MovementProfile(available_modes=[])


def test_movement_profile_ignores_stray_default_mode_key() -> None:
    """A product client that still sends `default_mode` should parse cleanly
    (Pydantic drops unknown keys). Keeps the cutover backwards-tolerant."""
    profile = MovementProfile.model_validate(
        {"default_mode": "transit", "available_modes": ["walking", "transit"]}
    )
    assert profile.available_modes == ["walking", "transit"]
    assert not hasattr(profile, "default_mode")
