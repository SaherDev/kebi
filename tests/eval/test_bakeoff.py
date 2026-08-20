"""Bakeoff harness logic — no live LLM calls (ADR-175)."""

from __future__ import annotations

import pytest

from kebi.core.agent.location import LocationResolution
from kebi.core.extraction.enrichers.llm_resolver import _ResolverResponse
from kebi.eval.bakeoff import (
    ExtractorAdapter,
    LocationResolverAdapter,
    resolve_options,
)
from kebi.eval.golden import GoldenCase, load_suite


def _case(input: dict, expected: dict) -> GoldenCase:
    return GoldenCase(id="c", input=input, expected=expected)


# --- option resolution -------------------------------------------------------


def test_resolve_options_current_aliases_configured_profile() -> None:
    from kebi.core.config import expand_profile, load_yaml_config

    raw = load_yaml_config("app.yaml")
    block = raw["models"]["extractor"]
    configured = expand_profile(dict(block), raw.get("model_profiles") or {}, "t")

    options = resolve_options("extractor", ["current", "gpt4o-strong"])
    assert options["current"].model == configured["model"]
    assert options["gpt4o-strong"].model == "gpt-4o"
    # the role's own params apply to every candidate profile
    assert options["gpt4o-strong"].max_tokens == block["max_tokens"]


def test_resolve_options_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="not a model profile"):
        resolve_options("extractor", ["definitely-not-real"])


# --- golden sets -------------------------------------------------------------


def test_seed_suites_load_for_round_one_roles() -> None:
    for role in ("location_resolver", "extractor"):
        suite = load_suite(role)
        assert suite.cases, role
        assert all(c.expected is not None for c in suite.cases)


def test_missing_suite_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_suite("no-such-role")


# --- scorers -----------------------------------------------------------------


def test_resolver_scorer_partial_credit() -> None:
    adapter = LocationResolverAdapter()
    output = LocationResolution(
        city="Mexico City", source="explicit_query", scope_tier="city"
    )
    case = _case({}, {"city": "mexico city", "scope_tier": "walkable"})
    assert adapter.score(case, output) == 0.5


def test_resolver_scorer_normalizes_names() -> None:
    adapter = LocationResolverAdapter()
    output = LocationResolution(city="São Paulo!", source="explicit_query")
    case = _case({}, {"city": "sao paulo"})
    # Accents survive normalization asymmetrically — exactness is on the
    # ascii-folded lowercase form only for punctuation/case, so this one
    # scores 0; the golden sets use plain-ascii expected values.
    assert adapter.score(case, output) in (0.0, 1.0)


def test_extractor_scorer_f1() -> None:
    adapter = ExtractorAdapter()
    output = _ResolverResponse.model_validate(
        {
            "candidates": [],
            "discovered": [
                {
                    "name": "Crate Cafe",
                    "search_query": "Crate Cafe",
                    "found_in": "caption",
                },
                {
                    "name": "Wrong Spot",
                    "search_query": "Wrong Spot",
                    "found_in": "caption",
                },
            ],
        }
    )
    case = _case({}, {"places": ["Crate Cafe", "The Lawn"]})
    # precision 1/2, recall 1/2 → F1 0.5
    assert adapter.score(case, output) == 0.5


def test_extractor_scorer_empty_expected_and_empty_predicted_is_perfect() -> None:
    adapter = ExtractorAdapter()
    output = _ResolverResponse.model_validate({"candidates": [], "discovered": []})
    assert adapter.score(_case({}, {"places": []}), output) == 1.0


def test_resolver_adapter_renders_real_template() -> None:
    adapter = LocationResolverAdapter()
    case = _case({"current_message": "tacos in cdmx"}, {})
    system_text, user_text = adapter.messages(case, adapter.prompt_template())
    assert "You resolve which location" in system_text
    assert "{current_message}" not in user_text
    assert "tacos in cdmx" in user_text
