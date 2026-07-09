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
        assert c.tool_timeouts_seconds.find_saved == 8
        assert c.find_saved.default_limit == 10
        assert c.find_saved.max_limit == 25

    def test_app_config_exposes_agent_with_defaults(self) -> None:
        cfg = get_config()
        assert cfg.agent.max_steps == 10
        assert cfg.agent.max_errors == 3
        assert cfg.agent.tool_timeouts_seconds.find_saved == 8

    def test_app_yaml_registers_agent_prompt(self) -> None:
        cfg = get_config()
        assert "agent" in cfg.prompts
        assert cfg.prompts["agent"].file == "agent.txt"

    def test_app_yaml_registers_extraction_prompts(self) -> None:
        """ADR-080: resolver + classifier prompts load from config."""
        cfg = get_config()
        assert cfg.prompts["place_resolver"].file == "place_resolver.txt"
        assert cfg.prompts["place_classifier"].file == "place_classifier.txt"
        assert cfg.prompts["place_resolver"].content.strip()
        assert "search_candidates" in cfg.prompts["place_classifier"].content


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
        assert t.find_saved == 8

    def test_rejects_zero_find_saved(self) -> None:
        with pytest.raises(ValidationError):
            ToolTimeoutsConfig(find_saved=0)


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

    def test_all_required_slots_present_loads_successfully(
        self, tmp_path: Path
    ) -> None:
        prompts_dir = tmp_path / "config" / "prompts"
        prompts_dir.mkdir(parents=True)
        # ADR-084 added {location_context} and {movement_context} to the
        # agent prompt's required slots.
        (prompts_dir / "agent.txt").write_text(
            "Location: {location_context}\n"
            "Movement: {movement_context}\n"
            "Taste: {taste_profile_summary}\n"
            "Memory: {memory_summary}"
        )

        import kebi.core.config as config_module

        original = config_module.find_project_root
        config_module.find_project_root = lambda: tmp_path  # type: ignore[assignment]
        try:
            loaded = _load_prompts({"agent": "agent.txt"})
            assert "agent" in loaded
            assert "{taste_profile_summary}" in loaded["agent"].content
            assert "{memory_summary}" in loaded["agent"].content
            assert "{movement_context}" in loaded["agent"].content
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


class TestGooglePlacesPricing:
    """Pin the recorded per-endpoint rates to the ADR-118 field masks:
    search = Text Search Pro, details = Place Details Essentials."""

    def test_search_endpoints_priced_at_pro(self) -> None:
        pricing = get_config().pricing.external.google_places
        assert pricing.cost_for(":searchText") == 0.032
        assert pricing.cost_for(":searchNearby") == 0.032

    def test_details_priced_at_essentials(self) -> None:
        pricing = get_config().pricing.external.google_places
        assert pricing.cost_for("/{place_id}") == 0.005

    def test_legacy_external_services_block_removed(self) -> None:
        from kebi.core.config import ExternalServicesConfig

        assert "google_places" not in ExternalServicesConfig.model_fields
