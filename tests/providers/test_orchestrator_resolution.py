"""AGENT_MODEL → orchestrator resolution (ADR-068, profile semantics per ADR-179).

`AGENT_MODEL` survives as the orchestrator's historical alias for
`KEBI_MODEL_ORCHESTRATOR`; its value is a PROFILE name from
`model_profiles` (option keys died with ADR-179's flat role blocks).
"""

import logging

import pytest

from kebi.core.config import (
    AppConfig,
    _load_prompts,
    _resolve_orchestrator,
    expand_profile,
    load_yaml_config,
)


def _raw_config() -> dict:
    """Load app.yaml and resolve prompt filenames, mirroring get_config()."""
    data = load_yaml_config("app.yaml")
    data["prompts"] = _load_prompts(data.get("prompts") or {})
    return data


def _orch_block() -> dict:
    return dict(load_yaml_config("app.yaml")["models"]["orchestrator"])


def _profiles() -> dict:
    return load_yaml_config("app.yaml").get("model_profiles") or {}


def _expand(block: dict) -> dict:
    return expand_profile(
        {k: v for k, v in block.items() if k != "advanced"}, _profiles(), "test"
    )


def test_agent_model_unset_resolves_to_configured_profile() -> None:
    orch = _orch_block()
    expected = _expand(orch)

    resolved = _resolve_orchestrator({"orchestrator": dict(orch)}, agent_model=None)

    assert resolved["orchestrator"]["provider"] == expected["provider"]
    assert resolved["orchestrator"]["model"] == expected["model"]


def test_agent_model_valid_resolves_to_named_profile() -> None:
    orch = _orch_block()
    # any profile other than the configured one — self-calibrating.
    other = next(k for k in _profiles() if k != orch["profile"])
    expected_model = _profiles()[other]["model"]

    resolved = _resolve_orchestrator({"orchestrator": dict(orch)}, agent_model=other)

    assert resolved["orchestrator"]["model"] == expected_model


def test_agent_model_invalid_warns_and_keeps_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    orch = _orch_block()
    expected = _expand(orch)

    with caplog.at_level(logging.WARNING, logger="kebi.core.config"):
        resolved = _resolve_orchestrator(
            {"orchestrator": dict(orch)}, agent_model="definitely-not-a-real-profile"
        )

    assert resolved["orchestrator"]["model"] == expected["model"]
    assert any(
        "definitely-not-a-real-profile" in rec.getMessage() for rec in caplog.records
    ), "expected a warning naming the bogus AGENT_MODEL value"


def test_unknown_configured_profile_refuses_to_boot() -> None:
    """A role whose `profile` names a missing catalog entry must refuse to
    boot (no silent fall-through)."""
    orch = _orch_block()
    orch["profile"] = "nonexistent-profile"

    with pytest.raises(ValueError, match="nonexistent-profile"):
        _resolve_orchestrator({"orchestrator": orch}, agent_model=None)


def test_appconfig_validator_resolves_without_env() -> None:
    """Direct `AppConfig(**raw)` (no get_config path) resolves profiles.

    Guards existing test helpers that bypass get_config().
    """
    raw = _raw_config()
    expected = _expand(_orch_block())

    cfg = AppConfig(**raw)

    assert cfg.models["orchestrator"].provider == expected["provider"]
    assert cfg.models["orchestrator"].model == expected["model"]


def test_advanced_key_emits_orchestrator_advanced_role() -> None:
    """`advanced` names the top tier's PROFILE — emitted as the separate
    `orchestrator_advanced` role with the same per-role params."""
    orch = _orch_block()
    advanced_model = _profiles()[orch["advanced"]]["model"]

    resolved = _resolve_orchestrator({"orchestrator": dict(orch)}, agent_model=None)

    assert resolved["orchestrator_advanced"]["model"] == advanced_model
    assert resolved["orchestrator_advanced"]["max_tokens"] == orch["max_tokens"]
    # the standard role still resolves independently to its own profile
    assert resolved["orchestrator"]["model"] == _expand(orch)["model"]


def test_advanced_pointing_at_missing_profile_raises() -> None:
    orch = _orch_block()
    orch["advanced"] = "nonexistent-profile"

    with pytest.raises(ValueError, match="nonexistent-profile"):
        _resolve_orchestrator({"orchestrator": orch}, agent_model=None)


def test_no_advanced_key_means_no_advanced_role() -> None:
    orch = _orch_block()
    orch.pop("advanced", None)

    resolved = _resolve_orchestrator({"orchestrator": orch}, agent_model=None)

    assert "orchestrator_advanced" not in resolved
