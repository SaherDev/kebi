"""Tests for AgentConfig / ToolTimeoutsConfig / agent prompt (feature 027 M2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kebi.core.config import (
    AgentConfig,
    ToolTimeoutsConfig,
    _load_prompts,
    get_config,
)


class TestAgentConfigDefaults:
    def test_default_instance(self) -> None:
        c = AgentConfig()
        assert c.max_steps == 10
        assert c.max_errors == 3
        assert c.checkpointer_ttl_seconds == 86400
        assert c.tool_timeouts_seconds.recall == 5
        assert c.tool_timeouts_seconds.consult == 10
        assert c.tool_timeouts_seconds.save == 60

    def test_app_config_exposes_agent_with_defaults(self) -> None:
        cfg = get_config()
        assert cfg.agent.max_steps == 10
        assert cfg.agent.max_errors == 3
        assert cfg.agent.tool_timeouts_seconds.recall == 5

    def test_app_yaml_registers_agent_prompt(self) -> None:
        cfg = get_config()
        assert "agent" in cfg.prompts
        assert cfg.prompts["agent"].file == "agent.txt"


class TestAgentConfigValidators:
    def test_rejects_zero_max_steps(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(max_steps=0)

    def test_rejects_zero_max_errors(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(max_errors=0)

    def test_rejects_zero_checkpointer_ttl(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(checkpointer_ttl_seconds=0)


class TestToolTimeoutsConfigValidators:
    def test_defaults(self) -> None:
        t = ToolTimeoutsConfig()
        assert t.recall == 5
        assert t.consult == 10
        assert t.save == 60

    def test_rejects_zero_recall(self) -> None:
        with pytest.raises(ValidationError):
            ToolTimeoutsConfig(recall=0)

    def test_rejects_zero_consult(self) -> None:
        with pytest.raises(ValidationError):
            ToolTimeoutsConfig(consult=0)

    def test_rejects_zero_save(self) -> None:
        with pytest.raises(ValidationError):
            ToolTimeoutsConfig(save=0)


class TestAgentPromptLoading:
    def test_agent_prompt_loads_with_both_slots(self) -> None:
        content = get_config().prompts["agent"].content
        assert "{taste_profile_summary}" in content
        assert "{memory_summary}" in content

    def test_agent_prompt_covers_places_range(self) -> None:
        """Regression guard against food-only persona drift (plan decision)."""
        content = get_config().prompts["agent"].content.lower()
        # At least 3 of these non-food place types must be mentioned.
        place_types = ["museum", "hotel", "shop", "bar", "cafe"]
        hits = sum(1 for p in place_types if p in content)
        assert hits >= 3, (
            f"agent.txt mentions {hits}/5 non-food place types; "
            f"expected ≥3 to avoid food-only drift"
        )

    def test_agent_prompt_includes_adr_044_mitigations(self) -> None:
        """Prompt must include ADR-044 prompt-injection mitigations."""
        content = get_config().prompts["agent"].content.lower()
        assert "untrusted" in content or "ignore" in content
        assert "<context>" in content

    def test_agent_prompt_does_not_reference_model_name(self) -> None:
        """Provider abstraction (Constitution III) — no model-name leaks."""
        content = get_config().prompts["agent"].content.lower()
        assert "claude" not in content
        assert "gpt" not in content
        assert "sonnet" not in content


class TestAgentPromptSlotValidation:
    def test_missing_slot_raises_on_load(self, tmp_path: Path) -> None:
        """_load_prompts aborts boot when a required slot is missing."""
        prompts_dir = tmp_path / "config" / "prompts"
        prompts_dir.mkdir(parents=True)
        # Missing {memory_summary} slot
        (prompts_dir / "agent.txt").write_text(
            "You are Kebi. Taste: {taste_profile_summary}. No memory slot here."
        )

        import kebi.core.config as config_module

        original = config_module.find_project_root
        config_module.find_project_root = lambda: tmp_path  # type: ignore[assignment]
        try:
            with pytest.raises(ValueError, match="missing required template slot"):
                _load_prompts({"agent": "agent.txt"})
        finally:
            config_module.find_project_root = original  # type: ignore[assignment]

    def test_both_slots_present_loads_successfully(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "config" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "agent.txt").write_text(
            "Taste: {taste_profile_summary}\nMemory: {memory_summary}"
        )

        import kebi.core.config as config_module

        original = config_module.find_project_root
        config_module.find_project_root = lambda: tmp_path  # type: ignore[assignment]
        try:
            loaded = _load_prompts({"agent": "agent.txt"})
            assert "agent" in loaded
            assert "{taste_profile_summary}" in loaded["agent"].content
            assert "{memory_summary}" in loaded["agent"].content
        finally:
            config_module.find_project_root = original  # type: ignore[assignment]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / "config" / "prompts"
        prompts_dir.mkdir(parents=True)

        import kebi.core.config as config_module

        original = config_module.find_project_root
        config_module.find_project_root = lambda: tmp_path  # type: ignore[assignment]
        try:
            with pytest.raises(FileNotFoundError, match="not found"):
                _load_prompts({"agent": "missing.txt"})
        finally:
            config_module.find_project_root = original  # type: ignore[assignment]
