"""Pure signal_counts aggregation from interaction rows (ADR-058).

aggregate_signal_counts() is a pure function — no I/O.

Positive types (save, accepted) feed the main tree.
Negative types (rejected) feed the rejected branch.
Source is counted for saves only.
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


class LocationContextCounts(BaseModel):
    neighborhood: dict[str, int] = Field(default_factory=dict)
    city: dict[str, int] = Field(default_factory=dict)
    country: dict[str, int] = Field(default_factory=dict)


class AttributeCounts(BaseModel):
    cuisine: dict[str, int] = Field(default_factory=dict)
    price_hint: dict[str, int] = Field(default_factory=dict)
    ambiance: dict[str, int] = Field(default_factory=dict)
    dietary: dict[str, int] = Field(default_factory=dict)
    good_for: dict[str, int] = Field(default_factory=dict)
    location_context: LocationContextCounts = Field(
        default_factory=LocationContextCounts
    )


class RejectedCounts(BaseModel):
    subcategory: dict[str, dict[str, int]] = Field(default_factory=dict)
    attributes: AttributeCounts = Field(default_factory=AttributeCounts)


class SignalCounts(BaseModel):
    totals: TotalCounts = Field(default_factory=TotalCounts)
    place_type: dict[str, int] = Field(default_factory=dict)
    subcategory: dict[str, dict[str, int]] = Field(default_factory=dict)
    source: dict[str, int] = Field(default_factory=dict)
    tags: dict[str, int] = Field(default_factory=dict)
    attributes: AttributeCounts = Field(default_factory=AttributeCounts)
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


def _increment_nested(
    d: dict[str, dict[str, int]], outer_key: str | None, inner_key: str | None
) -> None:
    """Increment count in a nested dict[str, dict[str, int]]."""
    if outer_key is not None and inner_key is not None:
        if outer_key not in d:
            d[outer_key] = {}
        d[outer_key][inner_key] = d[outer_key].get(inner_key, 0) + 1


def _add_attributes(target: AttributeCounts, row: InteractionRow) -> None:
    """Increment attribute counts from an interaction row."""
    attrs = row.attributes
    _increment(target.cuisine, attrs.cuisine)
    _increment(target.price_hint, attrs.price_hint)
    _increment(target.ambiance, attrs.ambiance)
    for d in attrs.dietary:
        _increment(target.dietary, d)
    for g in attrs.good_for:
        _increment(target.good_for, g)
    if attrs.location_context:
        lc = attrs.location_context
        _increment(target.location_context.neighborhood, lc.neighborhood)
        _increment(target.location_context.city, lc.city)
        _increment(target.location_context.country, lc.country)


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
            _increment(counts.place_type, row.place_type)
            _increment_nested(counts.subcategory, row.place_type, row.subcategory)
            for tag in row.tags:
                _increment(counts.tags, tag)
            _add_attributes(counts.attributes, row)

            # Source is save-only
            if row.type == "save":
                _increment(counts.source, row.source)

        elif row.type in _NEGATIVE_TYPES:
            # Rejected branch
            _increment_nested(
                counts.rejected.subcategory, row.place_type, row.subcategory
            )
            _add_attributes(counts.rejected.attributes, row)

    return counts
