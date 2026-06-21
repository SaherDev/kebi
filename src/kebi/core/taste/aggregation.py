"""Pure signal_counts aggregation from interaction rows (ADR-077).

aggregate_signal_counts() is a pure function — no I/O.

Positive types (save, accepted, saved_recommendation) feed the main tree.
Negative types (rejected) feed the rejected branch.
Source is counted for plain link-share saves only.

`weights` lets a positive type contribute more than one unit of evidence per
interaction: a `saved_recommendation` (the user kept a place kebi picked) is a
stronger taste signal than a passive link-share `save`, so it counts heavier
in the evidence tree. Totals stay raw event counts; only the category/tag/
location evidence is weighted. The default (no weights) leaves every type at
one — existing behaviour is unchanged.

Vocabulary is places-native: flat `categories`, typed tag dimensions
(cuisine/price/atmosphere/...), and location context. All count containers
stay nested `dict[str, int]` so the grounding validator's dotted-path walk
(regen._resolve_path) works unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from kebi.core.taste.schemas import InteractionRow  # noqa: TC001

# ---------------------------------------------------------------------------
# SignalCounts Pydantic models
# ---------------------------------------------------------------------------


class TotalCounts(BaseModel):
    saves: int = 0
    accepted: int = 0
    rejected: int = 0
    saved_recommendations: int = 0


class LocationCounts(BaseModel):
    neighborhood: dict[str, int] = Field(default_factory=dict)
    city: dict[str, int] = Field(default_factory=dict)
    country: dict[str, int] = Field(default_factory=dict)


class TagCounts(BaseModel):
    cuisine: dict[str, int] = Field(default_factory=dict)
    dietary: dict[str, int] = Field(default_factory=dict)
    feature: dict[str, int] = Field(default_factory=dict)
    atmosphere: dict[str, int] = Field(default_factory=dict)
    service: dict[str, int] = Field(default_factory=dict)
    price: dict[str, int] = Field(default_factory=dict)
    time: dict[str, int] = Field(default_factory=dict)
    season: dict[str, int] = Field(default_factory=dict)


class RejectedCounts(BaseModel):
    categories: dict[str, int] = Field(default_factory=dict)
    tags: TagCounts = Field(default_factory=TagCounts)
    location: LocationCounts = Field(default_factory=LocationCounts)


class SignalCounts(BaseModel):
    totals: TotalCounts = Field(default_factory=TotalCounts)
    categories: dict[str, int] = Field(default_factory=dict)
    source: dict[str, int] = Field(default_factory=dict)
    tags: TagCounts = Field(default_factory=TagCounts)
    location: LocationCounts = Field(default_factory=LocationCounts)
    rejected: RejectedCounts = Field(default_factory=RejectedCounts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POSITIVE_TYPES = {"save", "accepted", "saved_recommendation"}
_NEGATIVE_TYPES = {"rejected"}


def _increment(d: dict[str, int], key: str | None, n: int = 1) -> None:
    """Add `n` to the count for `key` in a dict[str, int], skipping None."""
    if key is not None:
        d[key] = d.get(key, 0) + n


def _add_tags(target: TagCounts, row: InteractionRow, n: int = 1) -> None:
    """Increment typed-tag counts from an interaction row by weight `n`."""
    for c in row.cuisine:
        _increment(target.cuisine, c, n)
    for v in row.dietary:
        _increment(target.dietary, v, n)
    for v in row.feature:
        _increment(target.feature, v, n)
    for v in row.atmosphere:
        _increment(target.atmosphere, v, n)
    for v in row.service:
        _increment(target.service, v, n)
    _increment(target.price, row.price, n)
    for v in row.time:
        _increment(target.time, v, n)
    for v in row.season:
        _increment(target.season, v, n)


def _add_location(target: LocationCounts, row: InteractionRow, n: int = 1) -> None:
    """Increment location-context counts from an interaction row by weight `n`."""
    _increment(target.neighborhood, row.neighborhood, n)
    _increment(target.city, row.city, n)
    _increment(target.country, row.country, n)


# ---------------------------------------------------------------------------
# Main aggregation function
# ---------------------------------------------------------------------------


def aggregate_signal_counts(
    rows: list[InteractionRow], weights: Mapping[str, int] | None = None
) -> SignalCounts:
    """Pure function: aggregate interaction rows into SignalCounts. No I/O.

    `weights` maps an interaction type to the evidence units it contributes
    per row (default 1). It amplifies only the category/tag/location tree —
    totals stay raw event counts and `source` stays one unit per save — so a
    heavier `saved_recommendation` weighs more on taste without distorting the
    headline counts.
    """
    weights = weights or {}
    counts = SignalCounts()

    for row in rows:
        weight = weights.get(row.type, 1)

        # --- Totals (raw event counts, unweighted) ---
        match row.type:
            case "save":
                counts.totals.saves += 1
            case "accepted":
                counts.totals.accepted += 1
            case "saved_recommendation":
                counts.totals.saved_recommendations += 1
            case "rejected":
                counts.totals.rejected += 1

        if row.type in _POSITIVE_TYPES:
            # Main tree — weighted by the signal's evidence strength
            for cat in row.categories:
                _increment(counts.categories, cat, weight)
            _add_tags(counts.tags, row, weight)
            _add_location(counts.location, row, weight)

            # Source is link-share-save only (kebi is not a discovery channel),
            # and unweighted — one save, one source observation.
            if row.type == "save":
                _increment(counts.source, row.source)

        elif row.type in _NEGATIVE_TYPES:
            # Rejected branch
            for cat in row.categories:
                _increment(counts.rejected.categories, cat, weight)
            _add_tags(counts.rejected.tags, row, weight)
            _add_location(counts.rejected.location, row, weight)

    return counts
