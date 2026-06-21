"""Tests for aggregate_signal_counts (ADR-077, places vocabulary)."""

from __future__ import annotations

from kebi.core.taste.aggregation import aggregate_signal_counts
from kebi.core.taste.schemas import InteractionRow

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _row(
    type: str = "save",
    categories: list[str] | None = None,
    cuisine: list[str] | None = None,
    dietary: list[str] | None = None,
    feature: list[str] | None = None,
    atmosphere: list[str] | None = None,
    service: list[str] | None = None,
    price: str | None = None,
    time: list[str] | None = None,
    season: list[str] | None = None,
    neighborhood: str | None = None,
    city: str | None = None,
    country: str | None = None,
    source: str | None = None,
) -> InteractionRow:
    return InteractionRow(
        type=type,
        categories=categories or ["restaurant"],
        cuisine=cuisine or [],
        dietary=dietary or [],
        feature=feature or [],
        atmosphere=atmosphere or [],
        service=service or [],
        price=price,
        time=time or [],
        season=season or [],
        neighborhood=neighborhood,
        city=city,
        country=country,
        source=source,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_rows() -> None:
    counts = aggregate_signal_counts([])

    assert counts.totals.saves == 0
    assert counts.totals.accepted == 0
    assert counts.totals.rejected == 0
    assert counts.categories == {}
    assert counts.source == {}
    assert counts.tags.cuisine == {}
    assert counts.location.city == {}


def test_save_only() -> None:
    rows = [
        _row(
            type="save",
            categories=["restaurant"],
            source="tiktok",
            cuisine=["italian"],
            price="moderate",
        ),
        _row(
            type="save",
            categories=["restaurant", "bar"],
            source="manual",
            cuisine=["japanese"],
            price="expensive",
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 2
    assert counts.totals.accepted == 0
    assert counts.categories == {"restaurant": 2, "bar": 1}
    assert counts.source == {"tiktok": 1, "manual": 1}
    assert counts.tags.cuisine == {"italian": 1, "japanese": 1}
    assert counts.tags.price == {"moderate": 1, "expensive": 1}


def test_mixed_types() -> None:
    rows = [
        _row(type="save", categories=["restaurant"]),
        _row(type="accepted", categories=["cafe"]),
        _row(type="rejected", categories=["bar"]),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 1
    assert counts.totals.accepted == 1
    assert counts.totals.rejected == 1

    # Positive types feed the main tree
    assert counts.categories == {"restaurant": 1, "cafe": 1}

    # Rejected feeds the rejected branch, not the main tree
    assert "bar" not in counts.categories
    assert counts.rejected.categories == {"bar": 1}


def test_rejection_feeds_rejected_branch() -> None:
    rows = [
        _row(
            type="rejected",
            categories=["restaurant"],
            cuisine=["american"],
            atmosphere=["casual"],
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.rejected == 1

    # Main tree empty
    assert counts.categories == {}
    assert counts.tags.cuisine == {}
    assert counts.tags.atmosphere == {}

    # Rejected branch populated
    assert counts.rejected.categories == {"restaurant": 1}
    assert counts.rejected.tags.cuisine == {"american": 1}
    assert counts.rejected.tags.atmosphere == {"casual": 1}


def test_source_counted_only_for_saves() -> None:
    rows = [
        _row(type="save", source="tiktok"),
        _row(type="accepted", source="instagram"),
    ]
    counts = aggregate_signal_counts(rows)

    # Only the save's source is counted
    assert counts.source == {"tiktok": 1}
    assert "instagram" not in counts.source


def test_multi_value_tag_dimensions_accumulate() -> None:
    rows = [
        _row(type="save", feature=["outdoor_seating", "dog_friendly"]),
        _row(type="accepted", feature=["outdoor_seating"], time=["late_night"]),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.tags.feature == {"outdoor_seating": 2, "dog_friendly": 1}
    assert counts.tags.time == {"late_night": 1}


def test_price_single_value_counted() -> None:
    rows = [
        _row(type="save", price="budget"),
        _row(type="save", price="budget"),
        _row(type="save", price="moderate"),
        _row(type="save", price=None),  # None skipped
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.tags.price == {"budget": 2, "moderate": 1}


def test_location_context() -> None:
    rows = [
        _row(
            type="save",
            neighborhood="Williamsburg",
            city="New York",
            country="US",
        ),
        _row(type="accepted", city="New York", country="US"),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.location.neighborhood == {"Williamsburg": 1}
    assert counts.location.city == {"New York": 2}
    assert counts.location.country == {"US": 2}


def test_saved_recommendation_feeds_positive_tree_and_own_total() -> None:
    """A saved_recommendation is a positive signal: it lands in the main tree
    (categories/tags/location) and its own total, separate from save/accepted."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["restaurant"],
            cuisine=["thai"],
            city="Bangkok",
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saved_recommendations == 1
    assert counts.totals.saves == 0
    assert counts.totals.accepted == 0
    # Default weight (no weights map) is 1.
    assert counts.categories == {"restaurant": 1}
    assert counts.tags.cuisine == {"thai": 1}
    assert counts.location.city == {"Bangkok": 1}


def test_saved_recommendation_does_not_count_source() -> None:
    """kebi is not a discovery channel — a saved_recommendation must not feed
    the source distribution (only a plain link-share save does)."""
    rows = [_row(type="saved_recommendation", source="kebi")]
    counts = aggregate_signal_counts(rows)

    assert counts.source == {}


def test_weight_amplifies_evidence_tree_not_totals() -> None:
    """A weight makes a saved_recommendation count heavier in the evidence tree
    while the headline total stays a raw event count."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["restaurant"],
            cuisine=["thai"],
            city="Bangkok",
        ),
    ]
    counts = aggregate_signal_counts(rows, weights={"saved_recommendation": 2})

    # Evidence tree is doubled...
    assert counts.categories == {"restaurant": 2}
    assert counts.tags.cuisine == {"thai": 2}
    assert counts.location.city == {"Bangkok": 2}
    # ...but the total remains one event.
    assert counts.totals.saved_recommendations == 1


def test_weight_leaves_other_types_at_one() -> None:
    """The weights map only affects the named type; save/accepted stay at 1."""
    rows = [
        _row(type="save", categories=["cafe"]),
        _row(type="accepted", categories=["bar"]),
        _row(type="saved_recommendation", categories=["restaurant"]),
    ]
    counts = aggregate_signal_counts(rows, weights={"saved_recommendation": 3})

    assert counts.categories == {"cafe": 1, "bar": 1, "restaurant": 3}


def test_accumulation_across_many_rows() -> None:
    """Existing keys must accumulate, not reset, as rows are added."""
    bangkok = {"city": "Bangkok", "country": "Thailand"}
    tokyo = {"city": "Tokyo", "country": "Japan"}

    rows = [
        _row(
            type="save",
            categories=["cafe"],
            source="tiktok",
            cuisine=["thai"],
            atmosphere=["trendy"],
            feature=["brunch"],
            **bangkok,
        ),
        _row(
            type="save",
            categories=["restaurant"],
            source="tiktok",
            cuisine=["thai"],
            feature=["group_friendly"],
            **bangkok,
        ),
        _row(
            type="save",
            categories=["restaurant"],
            source="tiktok",
            feature=["scenic_view"],
            **bangkok,
        ),
        _row(
            type="save",
            categories=["hotel"],
            source="instagram",
            **tokyo,
        ),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.totals.saves == 4
    assert counts.categories == {"cafe": 1, "restaurant": 2, "hotel": 1}
    assert counts.source == {"tiktok": 3, "instagram": 1}
    assert counts.location.city == {"Bangkok": 3, "Tokyo": 1}
    assert counts.location.country == {"Thailand": 3, "Japan": 1}
    assert counts.tags.cuisine == {"thai": 2}
    assert counts.tags.atmosphere == {"trendy": 1}
    assert counts.tags.feature == {
        "brunch": 1,
        "group_friendly": 1,
        "scenic_view": 1,
    }
