"""Tests for the controlled claim-tag vocabulary — normalization + rendering."""

from __future__ import annotations

from kebi.core.knowledge.tags import (
    CLAIM_TAG_TYPES,
    CLAIM_TAG_VALUES,
    normalize_claim_tags,
    render_claim_tag_vocabulary,
)


def test_practical_types_exist_with_stable_values() -> None:
    for type_name in ("money", "safety", "transport", "etiquette", "timing_trick"):
        assert type_name in CLAIM_TAG_TYPES
    # A few load-bearing values research + the prompt reference by name.
    for value in (
        "no_fee_atm",
        "cash_only",
        "tipping",
        "safe_at_night",
        "go_early",
        "walkable",
    ):
        assert value in CLAIM_TAG_VALUES


def test_places_enums_reused_not_duplicated() -> None:
    # Values imported from core/places/tags.py keep their exact spelling.
    assert "Thai" in CLAIM_TAG_VALUES
    assert "outdoor_seating" in CLAIM_TAG_VALUES
    assert "cozy" in CLAIM_TAG_VALUES
    assert "budget" in CLAIM_TAG_VALUES


def test_accessibility_categorically_excluded() -> None:
    assert "accessibility" not in CLAIM_TAG_TYPES
    for value in (
        "wheelchair_parking",
        "wheelchair_entrance",
        "wheelchair_restroom",
        "wheelchair_seating",
    ):
        assert value not in CLAIM_TAG_VALUES


def test_normalize_keeps_known_drops_unknown() -> None:
    assert normalize_claim_tags(["cash_only", "banana_pancakes", "tipping"]) == [
        "cash_only",
        "tipping",
    ]


def test_normalize_folds_case_space_and_hyphen_variants() -> None:
    assert normalize_claim_tags(["No-Fee ATM", "thai", "GO EARLY"]) == [
        "no_fee_atm",
        "Thai",
        "go_early",
    ]


def test_normalize_dedupes_preserving_order() -> None:
    assert normalize_claim_tags(["tipping", "Tipping", "cash_only", "tipping"]) == [
        "tipping",
        "cash_only",
    ]


def test_normalize_empty_and_all_unknown() -> None:
    assert normalize_claim_tags([]) == []
    assert normalize_claim_tags(["quantum", "vibes"]) == []


def test_render_lists_every_type_for_the_prompt() -> None:
    rendered = render_claim_tag_vocabulary()
    assert rendered.startswith("Allowed tag values")
    for type_name in CLAIM_TAG_TYPES:
        assert f"- {type_name}:" in rendered
    assert "no_fee_atm" in rendered
    # The rendered vocabulary never leaks the excluded accessibility values.
    assert "wheelchair" not in rendered
