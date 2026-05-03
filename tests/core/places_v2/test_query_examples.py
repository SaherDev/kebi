"""Smoke tests for query_examples.py.

Verifies that every named PlaceQuery in the file is valid (constructed without
error at import time) and passes basic structural invariants.
"""

from __future__ import annotations

import inspect

import pytest

import totoro_ai.core.places_v2.query_examples as examples
from totoro_ai.core.places_v2.models import PlaceQuery

# ---------------------------------------------------------------------------
# Collect all PlaceQuery instances defined at module level
# ---------------------------------------------------------------------------

_ALL_EXAMPLES: list[tuple[str, PlaceQuery]] = [
    (name, obj)
    for name, obj in inspect.getmembers(examples)
    if isinstance(obj, PlaceQuery)
]


def test_examples_not_empty() -> None:
    """Guard against a broken import silently producing zero examples."""
    assert len(_ALL_EXAMPLES) >= 10, f"Expected ≥10 examples, got {len(_ALL_EXAMPLES)}"


@pytest.mark.parametrize("name,query", _ALL_EXAMPLES, ids=[n for n, _ in _ALL_EXAMPLES])
def test_example_is_valid_place_query(name: str, query: PlaceQuery) -> None:
    assert isinstance(query, PlaceQuery), f"{name} should be a PlaceQuery"


@pytest.mark.parametrize("name,query", _ALL_EXAMPLES, ids=[n for n, _ in _ALL_EXAMPLES])
def test_geo_queries_have_radius(name: str, query: PlaceQuery) -> None:
    loc = query.location
    if loc and (loc.lat is not None or loc.lng is not None):
        assert loc.radius_m is not None, (
            f"{name}: location has lat/lng but no radius_m"
        )


@pytest.mark.parametrize("name,query", _ALL_EXAMPLES, ids=[n for n, _ in _ALL_EXAMPLES])
def test_tags_are_list_or_none(name: str, query: PlaceQuery) -> None:
    assert query.tags is None or isinstance(query.tags, list), (
        f"{name}: tags should be None or a list"
    )


# ---------------------------------------------------------------------------
# Spot-checks on a representative set
# ---------------------------------------------------------------------------

class TestSpotChecks:
    def test_find_thai_has_cuisine_tag(self) -> None:
        assert examples.find_thai.tags
        assert examples.find_thai.tags[0] == "Thai"

    def test_find_vegan_thai_has_two_tags(self) -> None:
        assert len(examples.find_vegan_thai.tags) == 2

    def test_nearby_vegan_has_geo(self) -> None:
        loc = examples.nearby_vegan.location
        assert loc is not None
        assert loc.lat == pytest.approx(13.7563)
        assert loc.radius_m == 1000

    def test_fully_accessible_has_three_accessibility_tags(self) -> None:
        assert len(examples.fully_accessible.tags) == 3

    def test_sukhumvit_japanese_neighbourhood_no_radius(self) -> None:
        loc = examples.sukhumvit_japanese.location
        assert loc is not None
        assert loc.neighborhood == "Sukhumvit"
        assert loc.lat is None
        assert loc.radius_m is None

    def test_winter_romantic_splurge_has_five_tags(self) -> None:
        assert len(examples.winter_romantic_splurge.tags) == 5
