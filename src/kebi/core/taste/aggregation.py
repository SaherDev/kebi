"""Pure signal_counts aggregation from interaction rows (ADR-077).

aggregate_signal_counts() is a pure function — no I/O.

Positive types (save, accepted) feed the main tree.
Negative types (rejected) feed the rejected branch.
Source is counted for saves only.

Vocabulary is places-native: flat `categories`, typed tag dimensions
(cuisine/price/atmosphere/...), and location context. All count containers
stay nested `dict[str, int]` so the grounding validator's dotted-path walk
(regen._resolve_path) works unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kebi.core.taste.schemas import InteractionRow  # noqa: TC001

# ---------------------------------------------------------------------------
# SignalCounts Pydantic models
# ---------------------------------------------------------------------------


class TotalCounts(BaseModel):
    saves: int = 0
    accepted: int = 0
    rejected: int = 0


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
    accessibility: dict[str, int] = Field(default_factory=dict)
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

_POSITIVE_TYPES = {"save", "accepted"}
_NEGATIVE_TYPES = {"rejected"}


def _increment(d: dict[str, int], key: str | None) -> None:
    """Increment count for key in a dict[str, int], skipping None."""
    if key is not None:
        d[key] = d.get(key, 0) + 1


def _add_tags(target: TagCounts, row: InteractionRow) -> None:
    """Increment typed-tag counts from an interaction row."""
    for c in row.cuisine:
        _increment(target.cuisine, c)
    for v in row.dietary:
        _increment(target.dietary, v)
    for v in row.feature:
        _increment(target.feature, v)
    for v in row.atmosphere:
        _increment(target.atmosphere, v)
    for v in row.service:
        _increment(target.service, v)
    _increment(target.price, row.price)
    for v in row.accessibility:
        _increment(target.accessibility, v)
    for v in row.time:
        _increment(target.time, v)
    for v in row.season:
        _increment(target.season, v)


def _add_location(target: LocationCounts, row: InteractionRow) -> None:
    """Increment location-context counts from an interaction row."""
    _increment(target.neighborhood, row.neighborhood)
    _increment(target.city, row.city)
    _increment(target.country, row.country)


# ---------------------------------------------------------------------------
# Main aggregation function
# ---------------------------------------------------------------------------


def aggregate_signal_counts(rows: list[InteractionRow]) -> SignalCounts:
    """Pure function: aggregate interaction rows into SignalCounts. No I/O."""
    counts = SignalCounts()

    for row in rows:
        # --- Totals ---
        match row.type:
            case "save":
                counts.totals.saves += 1
            case "accepted":
                counts.totals.accepted += 1
            case "rejected":
                counts.totals.rejected += 1

        if row.type in _POSITIVE_TYPES:
            # Main tree
            for cat in row.categories:
                _increment(counts.categories, cat)
            _add_tags(counts.tags, row)
            _add_location(counts.location, row)

            # Source is save-only
            if row.type == "save":
                _increment(counts.source, row.source)

        elif row.type in _NEGATIVE_TYPES:
            # Rejected branch
            for cat in row.categories:
                _increment(counts.rejected.categories, cat)
            _add_tags(counts.rejected.tags, row)
            _add_location(counts.rejected.location, row)

    return counts
