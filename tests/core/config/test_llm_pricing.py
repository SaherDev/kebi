"""LLM pricing config — cache-aware cost math + model-name lookup (ADR-172)."""

from __future__ import annotations

from kebi.core.config import LLMModelPricing, LLMRoleConfig, get_config


def _anthropic_entry() -> LLMModelPricing:
    return LLMModelPricing(
        input_per_1m=1.00,
        output_per_1m=5.00,
        cache_read_per_1m=0.10,
        cache_write_5m_per_1m=1.25,
    )


def test_cost_for_plain_tokens() -> None:
    entry = LLMModelPricing(input_per_1m=2.50, output_per_1m=10.00)
    cost = entry.cost_for({"input": 1_000_000, "output": 100_000})
    assert cost == 2.50 + 1.00


def test_cost_for_prices_cache_buckets_at_cache_rates() -> None:
    entry = _anthropic_entry()
    cost = entry.cost_for(
        {
            "input": 1_000_000,
            "output": 0,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        }
    )
    assert cost == 1.00 + 0.10 + 1.25


def test_cost_for_openai_cached_input_rate() -> None:
    entry = LLMModelPricing(
        input_per_1m=0.20, output_per_1m=1.20, cached_input_per_1m=0.02
    )
    cost = entry.cost_for(
        {"input": 0, "output": 0, "cache_read_input_tokens": 1_000_000}
    )
    assert cost == 0.02


def test_cost_for_unknown_cache_rate_falls_back_to_input_rate() -> None:
    # Overcounts rather than hides spend.
    entry = LLMModelPricing(input_per_1m=1.00, output_per_1m=5.00)
    cost = entry.cost_for(
        {"input": 0, "output": 0, "cache_read_input_tokens": 1_000_000}
    )
    assert cost == 1.00


def test_llm_cost_for_exact_key_beats_prefix() -> None:
    pricing = get_config().pricing
    # gpt-4o is a prefix of gpt-4o-mini; the mini key must win for mini.
    mini = pricing.llm_cost_for("gpt-4o-mini", {"input": 1_000_000, "output": 0})
    full = pricing.llm_cost_for("gpt-4o", {"input": 1_000_000, "output": 0})
    assert mini == 0.15
    assert full == 2.50


def test_llm_cost_for_prefix_matches_dated_model_id() -> None:
    pricing = get_config().pricing
    cost = pricing.llm_cost_for(
        "claude-haiku-4-5-20251001", {"input": 1_000_000, "output": 0}
    )
    assert cost == 1.00


def test_llm_cost_for_unknown_model_returns_none() -> None:
    pricing = get_config().pricing
    assert pricing.llm_cost_for("no-such-model", {"input": 10}) is None
    assert pricing.llm_cost_for(None, {"input": 10}) is None
    assert pricing.llm_cost_for("gpt-4o", None) is None


def test_every_configured_role_model_has_a_pricing_entry() -> None:
    """Every active LLM role must be priceable — a missing entry means
    silent unpriced spend. Non-token roles (embedder, transcriber) are
    priced by their own sections."""
    config = get_config()
    token_roles = {
        name: role
        for name, role in config.models.items()
        if name not in ("embedder", "transcriber")
    }
    for name, role in token_roles.items():
        cost = config.pricing.llm_cost_for(role.model, {"input": 1000, "output": 100})
        assert cost is not None, f"role {name} model {role.model} has no pricing entry"


def test_role_config_retry_and_timeout_defaults() -> None:
    role = LLMRoleConfig(provider="openai", model="gpt-4o-mini")
    assert role.max_retries == 2
    assert role.timeout_seconds is None
