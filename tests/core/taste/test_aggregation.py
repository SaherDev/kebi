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
    accessibility: list[str] | None = None,
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
        accessibility=accessibility or [],
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
