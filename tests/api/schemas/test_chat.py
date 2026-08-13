"""Tests for ChatRequest / ChatResponse schemas (ADR-065, ADR-084, ADR-085)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.api.schemas.chat import (
    ChatRequest,
    ChatResponse,
    MovementProfile,
    UserProfile,
)


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
    req = ChatRequest(message="hi")
    assert req.movement_profile is None


def test_chat_request_does_not_accept_user_id_field() -> None:
    """user_id arrives via X-Gateway-User-Id, not the body. Pydantic
    ignores unknown keys by default, so verify the field is absent on
    the parsed model rather than expecting a ValidationError."""
    req = ChatRequest.model_validate({"user_id": "u1", "message": "hi"})
    assert not hasattr(req, "user_id")
    assert req.message == "hi"


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


# --- UserProfile / about-me block (ADR-154) --------------------------------


def test_chat_request_user_profile_defaults_to_none() -> None:
    """A client that never sends a profile is a normal client, not an error."""
    req = ChatRequest(message="where should I eat")

    assert req.user_profile is None


def test_user_profile_accepts_a_partial_fill() -> None:
    """Every field is optional — a user who gave only a name is valid."""
    profile = UserProfile(call_me="Saher")

    assert profile.call_me == "Saher"
    assert profile.home_country is None
    assert profile.about is None


def test_home_country_is_normalized_to_upper() -> None:
    """`ae` and `AE` are one country, so they must be one stored value."""
    assert UserProfile(home_country="ae").home_country == "AE"


def test_home_country_rejects_names_and_alpha3() -> None:
    """The field is alpha-2 because entry rules turn on an exact country."""
    for bad in ("UAE", "United Arab Emirates", "A", "1E", ""):
        with pytest.raises(ValidationError):
            UserProfile(home_country=bad)


def test_blank_text_fields_become_none() -> None:
    """A cleared input arrives as "" and must not render as a stated fact."""
    profile = UserProfile(call_me="   ", about="\n")

    assert profile.call_me is None
    assert profile.about is None


def test_about_is_length_capped() -> None:
    """`about` is prompt weight on every request, so it is bounded at the edge."""
    with pytest.raises(ValidationError):
        UserProfile(about="x" * 301)


def test_call_me_is_length_capped() -> None:
    """A display name, not a paragraph smuggled through a small field."""
    with pytest.raises(ValidationError):
        UserProfile(call_me="x" * 41)
