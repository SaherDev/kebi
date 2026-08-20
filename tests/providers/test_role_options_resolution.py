"""Flat profile-reference resolution + KEBI_MODEL_<ROLE> overrides (ADR-179)."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from kebi.core.config import (
    _model_env_overrides,
    _resolve_model_options,
    expand_profile,
    load_yaml_config,
)


def _raw() -> dict:
    return load_yaml_config("app.yaml")


def _models_block() -> dict:
    return {k: dict(v) for k, v in _raw()["models"].items()}


def _profiles() -> dict:
    return _raw().get("model_profiles") or {}


def _resolve(models: dict, **kwargs) -> dict:
    return _resolve_model_options(models, profiles=_profiles(), **kwargs)


# --- resolution --------------------------------------------------------------


def test_no_overrides_resolves_every_role_to_its_configured_profile() -> None:
    models = _models_block()
    profiles = _profiles()
    expected = {
        role: expand_profile(
            {k: v for k, v in block.items() if k != "advanced"}, profiles, role
        )["model"]
        for role, block in models.items()
    }

    resolved = _resolve({k: dict(v) for k, v in models.items()})

    for role, model in expected.items():
        assert resolved[role]["model"] == model, role
    # `advanced` emits the separate advanced-tier role (ADR-068/177)
    advanced_profile = models["orchestrator"]["advanced"]
    assert (
        resolved["orchestrator_advanced"]["model"]
        == (profiles[advanced_profile]["model"])
    )


def test_override_selects_named_profile() -> None:
    resolved = _resolve(
        {"extractor": dict(_models_block()["extractor"])},
        overrides={"extractor": "gpt4o-strong"},
    )
    assert resolved["extractor"]["model"] == "gpt-4o"
    # per-role params survive a profile switch
    assert resolved["extractor"]["max_tokens"] == 512


def test_unknown_override_warns_and_keeps_configured_profile(
    caplog: pytest.LogCaptureFixture,
) -> None:
    block = dict(_models_block()["location_resolver"])
    with caplog.at_level(logging.WARNING, logger="kebi.core.config"):
        resolved = _resolve(
            {"location_resolver": dict(block)},
            overrides={"location_resolver": "not-a-profile"},
        )
    configured = expand_profile(block, _profiles(), "t")
    assert resolved["location_resolver"]["model"] == configured["model"]
    assert any(
        "KEBI_MODEL_LOCATION_RESOLVER" in rec.getMessage()
        and "not-a-profile" in rec.getMessage()
        for rec in caplog.records
    )


def test_kebi_model_orchestrator_beats_agent_model_alias() -> None:
    resolved = _resolve(
        {"orchestrator": dict(_models_block()["orchestrator"])},
        overrides={"orchestrator": "sonnet-strong"},
        agent_model="mini-workhorse",
    )
    assert resolved["orchestrator"]["model"] == "claude-sonnet-4-6"


def test_agent_model_alias_still_works_without_override() -> None:
    resolved = _resolve(
        {"orchestrator": dict(_models_block()["orchestrator"])},
        agent_model="sonnet-strong",
    )
    assert resolved["orchestrator"]["model"] == "claude-sonnet-4-6"
    # the advanced role is untouched by the default-tier override
    assert resolved["orchestrator_advanced"]["model"] == "claude-sonnet-5"


def test_advanced_on_non_orchestrator_role_raises() -> None:
    with pytest.raises(ValueError, match="reserved for the orchestrator"):
        _resolve({"extractor": {"profile": "mini-workhorse", "advanced": "sonnet-5"}})


def test_inline_provider_block_passes_through() -> None:
    flat = {"provider": "groq", "model": "whisper-large-v3-turbo", "max_tokens": 0}
    resolved = _resolve({"transcriber": dict(flat)})
    assert resolved["transcriber"]["model"] == "whisper-large-v3-turbo"


def test_model_env_overrides_reads_prefixed_vars() -> None:
    with patch.dict(
        os.environ,
        {
            "KEBI_MODEL_EXTRACTOR": "luna-workhorse",
            "KEBI_MODEL_LOCATION_RESOLVER": "qwen-flash",
        },
    ):
        overrides = _model_env_overrides()
    assert overrides["extractor"] == "luna-workhorse"
    assert overrides["location_resolver"] == "qwen-flash"


# --- catalog invariants (ADR-176/179) ---------------------------------------


def test_model_names_live_only_in_profiles() -> None:
    """The uniformity invariant: provider/model appear ONLY under
    model_profiles — every role block is a `profile:` reference plus
    per-role params. A family changes models by editing one profile."""
    for role, block in _raw()["models"].items():
        assert "provider" not in block, f"{role}: inline provider"
        assert "model" not in block, f"{role}: inline model"
        assert "profile" in block, f"{role}: missing profile reference"


def test_every_profile_reference_exists() -> None:
    profiles = set(_profiles())
    for role, block in _raw()["models"].items():
        assert block["profile"] in profiles, f"{role}: unknown profile"
        if "advanced" in block:
            assert block["advanced"] in profiles, f"{role}: unknown advanced profile"


def test_family_swap_is_one_profile_edit() -> None:
    """Editing model_profiles.mini-workhorse moves EVERY role in the family —
    the group-level change the profiles catalog exists for."""
    models = _models_block()
    profiles = dict(_profiles())
    profiles["mini-workhorse"] = {
        **profiles["mini-workhorse"],
        "model": "gpt-5.7-hypothetical",
    }

    resolved = _resolve_model_options(
        {k: dict(v) for k, v in models.items()}, profiles=profiles
    )

    family = [
        role
        for role, block in models.items()
        if block.get("profile") == "mini-workhorse"
    ]
    assert len(family) >= 10, "the mini family should cover the background roles"
    for role in family:
        assert resolved[role]["model"] == "gpt-5.7-hypothetical", role
    # roles outside the family are untouched
    baseline = _resolve({k: dict(v) for k, v in _models_block().items()})
    assert resolved["extractor"]["model"] == baseline["extractor"]["model"]


def test_role_keys_override_profile_keys() -> None:
    resolved = _resolve(
        {
            "taste_regen": {
                "profile": "mini-workhorse",
                "max_tokens": 99,
                "timeout_seconds": 5,
            }
        }
    )
    assert resolved["taste_regen"]["max_tokens"] == 99
    assert resolved["taste_regen"]["timeout_seconds"] == 5
    assert resolved["taste_regen"]["provider"] == "openai"  # from profile


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="unknown model profile"):
        _resolve({"taste_regen": {"profile": "nope"}})
