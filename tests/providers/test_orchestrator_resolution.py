"""Tests for AGENT_MODEL → orchestrator resolution (ADR-068)."""

import logging

import pytest

from kebi.core.config import (
    AppConfig,
    _load_prompts,
    _resolve_orchestrator,
    load_yaml_config,
)


def _raw_config() -> dict:
    """Load app.yaml and resolve prompt filenames, mirroring get_config()."""
    data = load_yaml_config("app.yaml")
    data["prompts"] = _load_prompts(data.get("prompts") or {})
    return data


def _orch_block() -> dict:
    """Shape-preserving copy of models.orchestrator from the real app.yaml."""
    data = load_yaml_config("app.yaml")
    return dict(data["models"]["orchestrator"])


def _raw_models_with_orch(orch: dict) -> dict:
    return {"orchestrator": dict(orch)}


def test_agent_model_unset_resolves_to_default() -> None:
    """AGENT_MODEL=None → orchestrator gets the option named by `default`."""
    orch = _orch_block()
    expected = orch[orch["default"]]

    resolved = _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)

    assert resolved["orchestrator"]["provider"] == expected["provider"]
    assert resolved["orchestrator"]["model"] == expected["model"]


def test_agent_model_valid_resolves_to_chosen_option() -> None:
    """AGENT_MODEL pointing at a real option key → that option wins."""
    orch = _orch_block()
    # Pick the first real option key so the test is self-calibrating against
    # whatever options app.yaml currently ships with. Skip the reserved
    # selector keys (`default`, `advanced`) and the option `default` names.
    reserved = {"default", "advanced", orch["default"]}
    non_default = next(k for k in orch if k not in reserved)
    expected = orch[non_default]

    resolved = _resolve_orchestrator(
        _raw_models_with_orch(orch), agent_model=non_default
    )

    assert resolved["orchestrator"]["provider"] == expected["provider"]
    assert resolved["orchestrator"]["model"] == expected["model"]


def test_agent_model_invalid_warns_and_falls_back_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown AGENT_MODEL → warning logged, default option chosen."""
    orch = _orch_block()
    expected = orch[orch["default"]]

    with caplog.at_level(logging.WARNING, logger="kebi.core.config"):
        resolved = _resolve_orchestrator(
            _raw_models_with_orch(orch), agent_model="definitely-not-a-real-option"
        )

    assert resolved["orchestrator"]["model"] == expected["model"]
    assert any(
        "definitely-not-a-real-option" in rec.getMessage() for rec in caplog.records
    ), "expected a warning naming the bogus AGENT_MODEL value"


def test_missing_default_key_raises() -> None:
    """An orchestrator block whose `default` names a missing option must
    refuse to boot (no silent fall-through)."""
    orch = _orch_block()
    orch["default"] = "nonexistent-option"

    with pytest.raises(ValueError, match="nonexistent-option"):
        _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)


def test_appconfig_validator_resolves_default_without_env() -> None:
    """Direct `AppConfig(**raw)` (no get_config path) resolves to default.

    Guards existing test helpers that bypass get_config().
    """
    raw = _raw_config()
    cfg = AppConfig(**raw)

    orch = _orch_block()
    expected = orch[orch["default"]]

    assert cfg.models["orchestrator"].provider == expected["provider"]
    assert cfg.models["orchestrator"].model == expected["model"]


def test_advanced_key_emits_orchestrator_advanced_role() -> None:
    """The `advanced` selector emits a separate `orchestrator_advanced` role
    from the same option list — no duplicate model definition."""
    orch = _orch_block()
    orch["advanced"] = "claude-sonnet"
    expected = orch["claude-sonnet"]

    resolved = _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)

    assert resolved["orchestrator_advanced"]["provider"] == expected["provider"]
    assert resolved["orchestrator_advanced"]["model"] == expected["model"]
    # The standard role still resolves independently to `default`.
    assert resolved["orchestrator"]["model"] == orch[orch["default"]]["model"]


def test_advanced_is_not_treated_as_a_model_option() -> None:
    """`advanced` is a reserved selector, not an option — pointing `default`
    at it would be invalid, proving it never enters the option set."""
    orch = _orch_block()
    orch["advanced"] = "claude-sonnet"
    orch["default"] = "advanced"

    with pytest.raises(ValueError, match="default"):
        _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)


def test_advanced_pointing_at_missing_option_raises() -> None:
    orch = _orch_block()
    orch["advanced"] = "nonexistent-option"

    with pytest.raises(ValueError, match="nonexistent-option"):
        _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)


def test_no_advanced_key_means_no_advanced_role() -> None:
    """Back-compat: an orchestrator block without `advanced` emits no
    `orchestrator_advanced` role (the agent path falls back to standard)."""
    orch = _orch_block()
    orch.pop("advanced", None)

    resolved = _resolve_orchestrator(_raw_models_with_orch(orch), agent_model=None)

    assert "orchestrator_advanced" not in resolved
