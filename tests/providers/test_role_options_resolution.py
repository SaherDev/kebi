"""Generic per-role option resolution + KEBI_MODEL_<ROLE> overrides (ADR-173)."""

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


def _profiles() -> dict:
    return load_yaml_config("app.yaml").get("model_profiles") or {}


def _models_block() -> dict:
    return {k: dict(v) for k, v in load_yaml_config("app.yaml")["models"].items()}


def _resolve(models: dict, **kwargs) -> dict:
    return _resolve_model_options(models, profiles=_profiles(), **kwargs)


def _optioned(role: str) -> dict:
    block = _models_block()[role]
    assert "default" in block, f"{role} is expected to be an optioned block"
    return block


def test_no_overrides_resolves_every_role_to_its_default() -> None:
    models = _models_block()
    profiles = _profiles()
    expected = {}
    for role, block in models.items():
        if "default" in block:
            option = expand_profile(block[block["default"]], profiles, role)
        else:
            option = expand_profile(block, profiles, role)
        expected[role] = option["model"]

    resolved = _resolve({k: dict(v) for k, v in models.items()})

    for role, model in expected.items():
        assert resolved[role]["model"] == model, role
    # advanced still emits the separate role (ADR-068)
    assert "orchestrator_advanced" in resolved


def test_override_selects_named_option() -> None:
    block = _optioned("extractor")
    resolved = _resolve({"extractor": dict(block)}, overrides={"extractor": "luna"})
    luna = expand_profile(block["luna"], _profiles(), "extractor.luna")
    assert resolved["extractor"]["model"] == luna["model"]


def test_unknown_override_warns_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    block = _optioned("location_resolver")
    with caplog.at_level(logging.WARNING, logger="kebi.core.config"):
        resolved = _resolve(
            {"location_resolver": dict(block)},
            overrides={"location_resolver": "not-an-option"},
        )
    default = expand_profile(block[block["default"]], _profiles(), "location_resolver")
    assert resolved["location_resolver"]["model"] == default["model"]
    assert any(
        "KEBI_MODEL_LOCATION_RESOLVER" in rec.getMessage()
        and "not-an-option" in rec.getMessage()
        for rec in caplog.records
    )


def test_kebi_model_orchestrator_beats_agent_model_alias() -> None:
    block = _optioned("orchestrator")
    resolved = _resolve(
        {"orchestrator": dict(block)},
        overrides={"orchestrator": "claude-sonnet"},
        agent_model="gpt-4o-mini",
    )
    sonnet = expand_profile(block["claude-sonnet"], _profiles(), "orch")
    assert resolved["orchestrator"]["model"] == sonnet["model"]


def test_agent_model_alias_still_works_without_override() -> None:
    block = _optioned("orchestrator")
    resolved = _resolve({"orchestrator": dict(block)}, agent_model="gpt-4o-mini")
    mini = expand_profile(block["gpt-4o-mini"], _profiles(), "orch")
    assert resolved["orchestrator"]["model"] == mini["model"]


def test_advanced_on_non_orchestrator_role_raises() -> None:
    block = {
        "default": "a",
        "advanced": "a",
        "a": {"provider": "openai", "model": "gpt-4o-mini"},
    }
    with pytest.raises(ValueError, match="reserved for the orchestrator"):
        _resolve_model_options({"extractor": block})


def test_flat_block_passes_through_untouched() -> None:
    flat = {"provider": "groq", "model": "whisper-large-v3-turbo", "max_tokens": 0}
    resolved = _resolve_model_options({"transcriber": dict(flat)})
    assert resolved["transcriber"]["model"] == "whisper-large-v3-turbo"


def test_model_env_overrides_reads_prefixed_vars() -> None:
    with patch.dict(
        os.environ,
        {"KEBI_MODEL_EXTRACTOR": "luna", "KEBI_MODEL_LOCATION_RESOLVER": "qwen-flash"},
    ):
        overrides = _model_env_overrides()
    assert overrides["extractor"] == "luna"
    assert overrides["location_resolver"] == "qwen-flash"


def test_staged_candidate_options_reference_configured_providers() -> None:
    """Every staged option must name a provider the factories support —
    a typo here would only surface at swap time, in prod."""
    supported = {"openai", "anthropic", "ollama", "groq", "openrouter"}
    for role, block in _models_block().items():
        if "default" not in block:
            continue
        for key, option in block.items():
            if key in ("default", "advanced"):
                continue
            expanded = expand_profile(option, _profiles(), f"{role}.{key}")
            assert expanded["provider"] in supported, f"{role}.{key}"


# --- model profiles (ADR-176) ------------------------------------------------


def test_model_names_live_only_in_profiles() -> None:
    """The uniformity invariant: provider/model appear ONLY under
    model_profiles — every role block and option is a `profile:` reference
    plus per-role params. A group changes models by editing one profile."""
    raw = load_yaml_config("app.yaml")
    for role, block in raw["models"].items():
        entries = (
            {role: block}
            if ("profile" in block or "provider" in block)
            else {
                f"{role}.{k}": v
                for k, v in block.items()
                if k not in ("default", "advanced")
            }
        )
        for where, entry in entries.items():
            assert "provider" not in entry, f"{where}: inline provider"
            assert "model" not in entry, f"{where}: inline model"
            assert "profile" in entry, f"{where}: missing profile reference"


def test_every_profile_is_referenced_and_every_reference_exists() -> None:
    raw = load_yaml_config("app.yaml")
    profiles = set(raw["model_profiles"])
    referenced: set[str] = set()
    for block in raw["models"].values():
        entries = (
            [block]
            if "profile" in block
            else [v for k, v in block.items() if k not in ("default", "advanced")]
        )
        for entry in entries:
            referenced.add(entry["profile"])
    assert referenced <= profiles, f"unknown profiles: {referenced - profiles}"
    assert profiles <= referenced, f"dead profiles: {profiles - referenced}"


def test_group_swap_is_one_profile_edit() -> None:
    """Editing model_profiles.mini-workhorse moves EVERY referencing role —
    the group-level change the profiles section exists for."""
    models = _models_block()
    profiles = dict(_profiles())
    profiles["mini-workhorse"] = {
        **profiles["mini-workhorse"],
        "model": "gpt-5.7-hypothetical",
    }

    resolved = _resolve_model_options(
        {k: dict(v) for k, v in models.items()}, profiles=profiles
    )

    moved = [
        role
        for role, block in models.items()
        if "default" in block
        and block[block["default"]].get("profile") == "mini-workhorse"
    ]
    assert len(moved) >= 8, "the mini group should cover the background roles"
    for role in moved:
        assert resolved[role]["model"] == "gpt-5.7-hypothetical", role
    # roles outside the group are untouched
    assert resolved["extractor"]["model"] == "gpt-4o"


def test_option_keys_override_profile_keys() -> None:
    resolved = _resolve_model_options(
        {
            "taste_regen": {
                "default": "a",
                "a": {"profile": "mini-workhorse", "max_tokens": 99, "model": "custom"},
            }
        },
        profiles=_profiles(),
    )
    assert resolved["taste_regen"]["model"] == "custom"
    assert resolved["taste_regen"]["max_tokens"] == 99
    assert resolved["taste_regen"]["provider"] == "openai"  # from profile


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="unknown model profile"):
        _resolve_model_options(
            {"taste_regen": {"default": "a", "a": {"profile": "nope"}}},
            profiles=_profiles(),
        )


def test_flat_block_may_reference_a_profile() -> None:
    resolved = _resolve_model_options(
        {"vision_frames": {"profile": "mini-workhorse", "max_tokens": 512}},
        profiles=_profiles(),
    )
    assert resolved["vision_frames"]["model"] == "gpt-4o-mini"
    assert resolved["vision_frames"]["max_tokens"] == 512
