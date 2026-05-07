"""Tests for aggregate_signal_counts (ADR-058)."""

from __future__ import annotations

from kebi.core.places.models import LocationContext, PlaceAttributes
from kebi.core.taste.aggregation import aggregate_signal_counts
from kebi.core.taste.schemas import InteractionRow

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _row(
    type: str = "save",
    place_type: str = "restaurant",
    subcategory: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    cuisine: str | None = None,
    price_hint: str | None = None,
    ambiance: str | None = None,
    dietary: list[str] | None = None,
    good_for: list[str] | None = None,
    location_context: LocationContext | None = None,
) -> InteractionRow:
    return InteractionRow(
        type=type,
        place_type=place_type,
        subcategory=subcategory,
        source=source,
        tags=tags or [],
        attributes=PlaceAttributes(
            cuisine=cuisine,
            price_hint=price_hint,
            ambiance=ambiance,
            dietary=dietary or [],
            good_for=good_for or [],
            location_context=location_context,
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_rows() -> None:
    counts = aggregate_signal_counts([])

    assert counts.totals.saves == 0
    assert counts.totals.accepted == 0
    assert counts.totals.rejected == 0
    assert counts.place_type == {}
    assert counts.subcategory == {}
    assert counts.source == {}
    assert counts.tags == {}


def test_save_only() -> None:
    rows = [
        _row(
            type="save",
            place_type="restaurant",
            subcategory="italian",
            source="google_maps",
            cuisine="italian",
            price_hint="$$",
        ),
        _row(
            type="save",
            place_type="restaurant",
            subcategory="japanese",
            source="manual",
            cuisine="japanese",
            price_hint="$$$",
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 2
    assert counts.totals.accepted == 0
    assert counts.place_type == {"restaurant": 2}
    assert counts.subcategory == {
        "restaurant": {"italian": 1, "japanese": 1},
    }
    assert counts.source == {"google_maps": 1, "manual": 1}
    assert counts.attributes.cuisine == {"italian": 1, "japanese": 1}
    assert counts.attributes.price_hint == {"$$": 1, "$$$": 1}


def test_mixed_types() -> None:
    rows = [
        _row(type="save", place_type="restaurant", subcategory="thai"),
        _row(type="accepted", place_type="cafe", subcategory="coffee_shop"),
        _row(type="rejected", place_type="bar", subcategory="dive_bar"),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 1
    assert counts.totals.accepted == 1
    assert counts.totals.rejected == 1

    # Positive types feed main tree
    assert counts.place_type == {"restaurant": 1, "cafe": 1}
    assert "restaurant" in counts.subcategory
    assert "cafe" in counts.subcategory

    # Rejected feeds rejected branch, not main tree
    assert "bar" not in counts.place_type
    assert "bar" in counts.rejected.subcategory


def test_rejection_feeds_rejected_branch() -> None:
    rows = [
        _row(
            type="rejected",
            place_type="restaurant",
            subcategory="fast_food",
            cuisine="american",
            ambiance="casual",
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.rejected == 1

    # Main tree should be empty
    assert counts.place_type == {}
    assert counts.subcategory == {}
    assert counts.attributes.cuisine == {}
    assert counts.attributes.ambiance == {}

    # Rejected branch populated
    assert counts.rejected.subcategory == {
        "restaurant": {"fast_food": 1},
    }
    assert counts.rejected.attributes.cuisine == {"american": 1}
    assert counts.rejected.attributes.ambiance == {"casual": 1}


def test_source_counted_only_for_saves() -> None:
    rows = [
        _row(type="save", source="google_maps"),
        _row(type="accepted", source="recommendation"),
    ]
    counts = aggregate_signal_counts(rows)

    # Only the save's source is counted
    assert counts.source == {"google_maps": 1}
    assert "recommendation" not in counts.source


def test_multiple_tags() -> None:
    rows = [
        _row(type="save", tags=["date-night", "outdoor-seating"]),
        _row(type="accepted", tags=["date-night", "romantic"]),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.tags == {"date-night": 2, "outdoor-seating": 1, "romantic": 1}


def test_location_context() -> None:
    loc = LocationContext(neighborhood="Williamsburg", city="New York", country="US")
    rows = [
        _row(type="save", location_context=loc),
        _row(
            type="accepted",
            location_context=LocationContext(city="New York", country="US"),
        ),
    ]
    counts = aggregate_signal_counts(rows)

    lc = counts.attributes.location_context
    assert lc.neighborhood == {"Williamsburg": 1}
    assert lc.city == {"New York": 2}
    assert lc.country == {"US": 2}


def test_nested_increment_preserves_existing_outer_keys() -> None:
    """Adding a new outer key (place_type) must not wipe existing ones.

    Regression: guards `_increment_nested` against overriding existing nested
    dicts. Simulates 3 Bangkok/food_and_drink saves + 1 Tokyo/accommodation save.
    """
    bangkok = LocationContext(city="Bangkok", country="Thailand")
    tokyo = LocationContext(city="Tokyo", country="Japan")

    rows = [
        _row(
            type="save",
            place_type="food_and_drink",
            subcategory="cafe",
            source="tiktok",
            cuisine="thai",
            ambiance="trendy",
            good_for=["brunch"],
            location_context=bangkok,
        ),
        _row(
            type="save",
            place_type="food_and_drink",
            subcategory="restaurant",
            source="tiktok",
            cuisine="thai",
            good_for=["groups"],
            location_context=bangkok,
        ),
        _row(
            type="save",
            place_type="food_and_drink",
            subcategory="restaurant",
            source="tiktok",
            good_for=["sunset"],
            location_context=bangkok,
        ),
        _row(
            type="save",
            place_type="accommodation",
            subcategory="hotel",
            source="instagram",
            tags=["tokyohotel", "japantravel", "tokyotravel"],
            location_context=tokyo,
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 4
    assert counts.place_type == {"food_and_drink": 3, "accommodation": 1}

    # Outer key food_and_drink must still hold its inner counts after the
    # accommodation row is added.
    assert counts.subcategory == {
        "food_and_drink": {"cafe": 1, "restaurant": 2},
        "accommodation": {"hotel": 1},
    }

    assert counts.source == {"tiktok": 3, "instagram": 1}
    assert counts.tags == {"tokyohotel": 1, "japantravel": 1, "tokyotravel": 1}

    # Existing location entries must not be clobbered when a new city is added.
    lc = counts.attributes.location_context
    assert lc.city == {"Bangkok": 3, "Tokyo": 1}
    assert lc.country == {"Thailand": 3, "Japan": 1}

    # Attribute dicts accumulate across rows rather than being replaced.
    assert counts.attributes.cuisine == {"thai": 2}
    assert counts.attributes.ambiance == {"trendy": 1}
    assert counts.attributes.good_for == {
        "brunch": 1,
        "groups": 1,
        "sunset": 1,
    }


def test_nested_increment_repeated_outer_inner_pair() -> None:
    """Same (outer_key, inner_key) pair repeated must increment, not reset."""
    rows = [
        _row(type="save", place_type="food_and_drink", subcategory="cafe"),
        _row(type="save", place_type="food_and_drink", subcategory="cafe"),
        _row(type="save", place_type="food_and_drink", subcategory="cafe"),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.subcategory == {"food_and_drink": {"cafe": 3}}
