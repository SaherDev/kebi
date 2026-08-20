"""Generic per-role option resolution + KEBI_MODEL_<ROLE> overrides (ADR-173)."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from kebi.core.config import (
    _model_env_overrides,
    _resolve_model_options,
    load_yaml_config,
)


def _models_block() -> dict:
    return {k: dict(v) for k, v in load_yaml_config("app.yaml")["models"].items()}


def _optioned(role: str) -> dict:
    block = _models_block()[role]
    assert "default" in block, f"{role} is expected to be an optioned block"
    return block


def test_no_overrides_resolves_every_role_to_its_default() -> None:
    models = _models_block()
    expected = {}
    for role, block in models.items():
        if "default" in block:
            expected[role] = block[block["default"]]["model"]
        else:
            expected[role] = block["model"]

    resolved = _resolve_model_options({k: dict(v) for k, v in models.items()})

    for role, model in expected.items():
        assert resolved[role]["model"] == model, role
    # advanced still emits the separate role (ADR-068)
    assert "orchestrator_advanced" in resolved


def test_override_selects_named_option() -> None:
    block = _optioned("extractor")
    resolved = _resolve_model_options(
        {"extractor": dict(block)}, overrides={"extractor": "luna"}
    )
    assert resolved["extractor"]["model"] == block["luna"]["model"]


def test_unknown_override_warns_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    block = _optioned("location_resolver")
    with caplog.at_level(logging.WARNING, logger="kebi.core.config"):
        resolved = _resolve_model_options(
            {"location_resolver": dict(block)},
            overrides={"location_resolver": "not-an-option"},
        )
    assert resolved["location_resolver"]["model"] == (block[block["default"]]["model"])
    assert any(
        "KEBI_MODEL_LOCATION_RESOLVER" in rec.getMessage()
        and "not-an-option" in rec.getMessage()
        for rec in caplog.records
    )


def test_kebi_model_orchestrator_beats_agent_model_alias() -> None:
    block = _optioned("orchestrator")
    resolved = _resolve_model_options(
        {"orchestrator": dict(block)},
        overrides={"orchestrator": "claude-sonnet"},
        agent_model="gpt-4o-mini",
    )
    assert resolved["orchestrator"]["model"] == block["claude-sonnet"]["model"]


def test_agent_model_alias_still_works_without_override() -> None:
    block = _optioned("orchestrator")
    resolved = _resolve_model_options(
        {"orchestrator": dict(block)}, agent_model="gpt-4o-mini"
    )
    assert resolved["orchestrator"]["model"] == block["gpt-4o-mini"]["model"]


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
            assert option["provider"] in supported, f"{role}.{key}"
