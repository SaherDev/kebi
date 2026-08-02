"""Tests for aggregate_signal_counts — the pill-overlay conviction ladder.

A passive link-share `save` trains nothing on its own; a `saved_recommendation`
carries a base weight; the Library pills `visited`/`liked=True` add graduated
bonuses on top of a saved place; `approved=False` excludes a place entirely;
`liked=False` routes it to the rejected branch and overrides any positive; and a
place is emitted as evidence once even across two origin rows (ADR-115).
"""

from __future__ import annotations

from kebi.core.taste.aggregation import aggregate_signal_counts
from kebi.core.taste.schemas import InteractionRow

# The config defaults (config/app.yaml taste_model.signal_weights).
WEIGHTS = {
    "save": 0,
    "accepted": 1,
    "saved_recommendation": 2,
    "visited": 2,
    "liked": 3,
    "liked_negative": 3,
    "rejected": 1,
    "area_interest": 2,
    "experience": 2,
}


def _row(
    type: str = "save",
    *,
    place_core_id: str | None = None,
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
    approved: bool = True,
    visited: bool = False,
    liked: bool | None = None,
) -> InteractionRow:
    return InteractionRow(
        type=type,
        place_core_id=place_core_id,
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
        approved=approved,
        visited=visited,
        liked=liked,
    )


# ---------------------------------------------------------------------------
# The conviction ladder — one saved place at each rung
# ---------------------------------------------------------------------------


def test_empty_rows() -> None:
    counts = aggregate_signal_counts([], weights=WEIGHTS)

    assert counts.totals.saves == 0
    assert counts.categories == {}
    assert counts.source == {}


def test_neutral_link_share_save_trains_nothing() -> None:
    """A passive save, approved but not visited/liked, is weight 0 — it records
    its source and total but adds no evidence to the tree."""
    rows = [_row(type="save", categories=["restaurant"], source="tiktok")]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.saves == 1
    assert counts.source == {"tiktok": 1}
    assert counts.categories == {}  # weight 0 — no evidence


def test_saved_recommendation_neutral_is_base_two() -> None:
    rows = [_row(type="saved_recommendation", categories=["restaurant"])]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.saved_recommendations == 1
    assert counts.categories == {"restaurant": 2}


def test_save_plus_liked_is_three() -> None:
    """A link-share you loved outweighs a neutral saved_recommendation (2)."""
    rows = [_row(type="save", categories=["restaurant"], liked=True)]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.categories == {"restaurant": 3}


def test_save_plus_visited_and_liked_is_five() -> None:
    rows = [
        _row(type="save", categories=["restaurant"], visited=True, liked=True),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.categories == {"restaurant": 5}


def test_saved_recommendation_visited_and_liked_is_seven() -> None:
    """Top of the ladder: kebi picked it, you went, you loved it."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["restaurant"],
            visited=True,
            liked=True,
        ),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.categories == {"restaurant": 7}


def test_accepted_is_base_one() -> None:
    rows = [_row(type="accepted", categories=["bar"])]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.accepted == 1
    assert counts.categories == {"bar": 1}


# ---------------------------------------------------------------------------
# Curation gate + negatives
# ---------------------------------------------------------------------------


def test_unapproved_place_is_excluded_even_when_liked() -> None:
    """approved=False is the curation gate — a needs-review place is not
    evidence, positive or negative, regardless of pills."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["restaurant"],
            approved=False,
            liked=True,
        ),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.saved_recommendations == 1  # event still counted
    assert counts.categories == {}
    assert counts.rejected.categories == {}


def test_disliked_place_feeds_rejected_and_overrides_base() -> None:
    """liked=False on a saved_recommendation: the base-2 positive is discarded
    and the place lands only in the rejected branch."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["restaurant"],
            cuisine=["thai"],
            liked=False,
        ),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.categories == {}
    assert counts.tags.cuisine == {}
    assert counts.rejected.categories == {"restaurant": 3}  # liked_negative
    assert counts.rejected.tags.cuisine == {"thai": 3}


def test_rejected_rec_feeds_rejected_branch() -> None:
    rows = [_row(type="rejected", categories=["bar"], atmosphere=["loud"])]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.rejected == 1
    assert counts.categories == {}
    assert counts.rejected.categories == {"bar": 1}
    assert counts.rejected.tags.atmosphere == {"loud": 1}


# ---------------------------------------------------------------------------
# Source, dedup, accumulation
# ---------------------------------------------------------------------------


def test_source_counted_only_for_link_share_saves() -> None:
    rows = [
        _row(type="save", source="tiktok", liked=True),
        _row(type="saved_recommendation", source="kebi"),
        _row(type="accepted", source="instagram"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.source == {"tiktok": 1}


def test_evidence_deduped_once_per_place_totals_count_both() -> None:
    """A place carrying both a link-share `save` and a later
    `saved_recommendation` row (same place_core_id) is emitted as evidence once
    — preferring the stronger saved_recommendation base — while both events
    still count in totals."""
    rows = [
        _row(type="save", place_core_id="p1", categories=["restaurant"]),
        _row(
            type="saved_recommendation",
            place_core_id="p1",
            categories=["restaurant"],
        ),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.saves == 1
    assert counts.totals.saved_recommendations == 1
    # Evidence applied once, at the saved_recommendation base (2), not 0+2.
    assert counts.categories == {"restaurant": 2}


def test_accumulation_across_many_rows() -> None:
    """Keys accumulate across rows; the ladder composes per place."""
    rows = [
        _row(
            type="saved_recommendation",
            categories=["cafe"],
            cuisine=["thai"],
            city="Bangkok",
        ),
        _row(
            type="save",
            categories=["restaurant"],
            liked=True,
            city="Bangkok",
        ),
        _row(type="accepted", categories=["restaurant"], city="Tokyo"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.totals.saved_recommendations == 1
    assert counts.totals.saves == 1
    assert counts.totals.accepted == 1
    # cafe: 2 (saved_rec), restaurant: 3 (liked save) + 1 (accepted) = 4
    assert counts.categories == {"cafe": 2, "restaurant": 4}
    assert counts.tags.cuisine == {"thai": 2}
    assert counts.location.city == {"Bangkok": 5, "Tokyo": 1}


def test_price_single_value_weighted() -> None:
    rows = [
        _row(type="saved_recommendation", price="budget"),
        _row(type="accepted", price="budget"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    # budget: 2 (saved_rec) + 1 (accepted) = 3
    assert counts.tags.price == {"budget": 3}


def test_default_weights_save_neutral_accepted_one() -> None:
    """With no weights map, the built-in fallbacks apply: a bare `save` is
    evidence-neutral (get('save', 0) == 0) while `accepted` is 1."""
    rows = [
        _row(type="save", categories=["cafe"]),
        _row(type="accepted", categories=["bar"]),
    ]
    counts = aggregate_signal_counts(rows)

    assert counts.categories == {"bar": 1}
    assert "cafe" not in counts.categories


# ---------------------------------------------------------------------------
# Location-kinds Step 3 — region-interest & experience signals
# ---------------------------------------------------------------------------


def test_area_interest_fills_region_bucket_and_total() -> None:
    """An area_interest row lands in `region_interest` (keyed by name) and the
    `area_interests` total — never in the venue-derived `location`."""
    rows = [
        InteractionRow(type="area_interest", region="Hoi An"),
        InteractionRow(type="area_interest", region="Vietnam"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.region_interest == {"Hoi An": 2, "Vietnam": 2}
    assert counts.totals.area_interests == 2
    # Kept distinct from a venue save's location context.
    assert counts.location.city == {}
    assert counts.location.country == {}


def test_repeated_area_shares_accumulate() -> None:
    """No once-per dedup for regions: sharing the same area twice is a louder
    signal, not a merged one."""
    rows = [
        InteractionRow(type="area_interest", region="Hoi An"),
        InteractionRow(type="area_interest", region="Hoi An"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)
    assert counts.region_interest == {"Hoi An": 4}


def test_region_interest_never_merges_with_venue_location() -> None:
    """A venue saved in a city and a region-interest of the same-named area
    stay in separate buckets — the done-when distinctness guarantee."""
    rows = [
        _row(type="saved_recommendation", city="Hoi An", approved=True),
        InteractionRow(type="area_interest", region="Hoi An"),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.location.city == {"Hoi An": 2}  # from the venue save
    assert counts.region_interest == {"Hoi An": 2}  # from the area share
    # Nothing crossed over.
    assert "Hoi An" not in counts.region_interest.keys() - {"Hoi An"}


def test_experience_fills_experience_bucket() -> None:
    """An experience_interest row spreads its tags across the experience
    bucket; it carries no place and no total."""
    rows = [
        InteractionRow(
            type="experience_interest",
            experience=["scenic_route", "motorbike_route"],
        ),
        InteractionRow(type="experience_interest", experience=["scenic_route"]),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.experience == {"scenic_route": 4, "motorbike_route": 2}
    assert counts.totals.area_interests == 0


def test_area_and_experience_are_positive_only() -> None:
    """Neither signal ever touches the rejected branch."""
    rows = [
        InteractionRow(type="area_interest", region="Hoi An"),
        InteractionRow(type="experience_interest", experience=["hiking"]),
    ]
    counts = aggregate_signal_counts(rows, weights=WEIGHTS)

    assert counts.rejected.categories == {}
    assert counts.rejected.location.city == {}


def test_zero_weight_silences_region_bucket() -> None:
    """A 0 weight leaves the bucket empty (config lever to mute the signal)."""
    rows = [InteractionRow(type="area_interest", region="Hoi An")]
    counts = aggregate_signal_counts(rows, weights={**WEIGHTS, "area_interest": 0})
    assert counts.region_interest == {}
    # Still counted as a raw event.
    assert counts.totals.area_interests == 1
