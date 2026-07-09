"""Tests for the safety-hard / rest-soft constraint split (ADR-118)."""

from __future__ import annotations

from kebi.core.agent.tools._hard_constraints import (
    SAFETY_TAG_VALUES,
    hard_constraints_satisfied,
    split_constraints,
)
from kebi.core.places.models import PlaceCore, PlaceTag


def _place(*tag_values: str) -> PlaceCore:
    return PlaceCore(
        place_name="P",
        tags=[PlaceTag(type="feature", value=v, source="llm") for v in tag_values],
    )


class TestSplitConstraints:
    def test_dietary_values_are_hard(self) -> None:
        hard, soft = split_constraints(["vegetarian", "vegan", "halal"])
        assert hard == ["vegetarian", "vegan", "halal"]
        assert soft == []

    def test_accessibility_values_are_hard(self) -> None:
        hard, soft = split_constraints(["wheelchair_entrance", "rooftop"])
        assert hard == ["wheelchair_entrance"]
        assert soft == ["rooftop"]

    def test_service_feature_price_are_soft(self) -> None:
        hard, soft = split_constraints(
            ["serves_dinner", "outdoor_seating", "budget", "cozy"]
        )
        assert hard == []
        assert soft == ["serves_dinner", "outdoor_seating", "budget", "cozy"]

    def test_case_insensitive_and_whitespace(self) -> None:
        hard, soft = split_constraints(["  Vegetarian ", "ROOFTOP"])
        assert hard == ["  Vegetarian "]
        assert soft == ["ROOFTOP"]

    def test_unknown_free_text_values_are_soft(self) -> None:
        """An unrecognized value must never become an accidental excluder."""
        hard, soft = split_constraints(["michelin_starred", "pet_cafe"])
        assert hard == []
        assert soft == ["michelin_starred", "pet_cafe"]

    def test_empty_input(self) -> None:
        assert split_constraints([]) == ([], [])

    def test_safety_set_covers_both_axes(self) -> None:
        assert "vegetarian_options" in SAFETY_TAG_VALUES
        assert "wheelchair_restroom" in SAFETY_TAG_VALUES
        assert "rooftop" not in SAFETY_TAG_VALUES
        assert "budget" not in SAFETY_TAG_VALUES


class TestHardFilterWithSplit:
    def test_fresh_place_survives_soft_only_constraints(self) -> None:
        """A tag-less fresh place passes when all requested values are soft."""
        hard, _soft = split_constraints(["serves_dinner", "rooftop"])
        assert hard_constraints_satisfied(_place(), hard) is True

    def test_fresh_place_dropped_on_missing_safety_value(self) -> None:
        hard, _soft = split_constraints(["vegetarian", "rooftop"])
        assert hard_constraints_satisfied(_place("rooftop"), hard) is False

    def test_safety_value_present_passes(self) -> None:
        hard, _soft = split_constraints(["vegetarian"])
        assert hard_constraints_satisfied(_place("vegetarian"), hard) is True
