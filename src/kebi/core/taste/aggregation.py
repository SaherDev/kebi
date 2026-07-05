"""Pure signal_counts aggregation from interaction rows (ADR-077).

aggregate_signal_counts() is a pure function — no I/O.

Evidence is conviction-ranked. `accepted` feeds the main tree at the base
weight. Saved places (`save` / `saved_recommendation`) are governed by their
Library-pill snapshot: a passive link-share `save` is worth nothing on its own
(weight 0 — we save a lot but with low conviction), a `saved_recommendation`
carries a base weight, and `visited` / `liked=True` add graduated bonuses on
top, so a place you approved, went to, and loved outweighs everything. An
un-approved (needs-review) place is not evidence at all — `approved` is a
curation gate, not a sentiment. `liked=False` is a real negative and routes the
place to the rejected branch, overriding any positive. `rejected` recs feed the
rejected branch too. Totals stay raw event counts; source is counted for plain
link-share saves only, unweighted. A place is emitted as evidence once even if
it carries both a `save` and a later `saved_recommendation` row.

`weights` maps each lever (`save`, `accepted`, `saved_recommendation`,
`visited`, `liked`, `liked_negative`, `rejected`) to its evidence units.

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

# Saved-place rows: governed by the pill snapshot (approved/visited/liked).
_SAVE_TYPES = {"save", "saved_recommendation"}


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


def _apply_positive(counts: SignalCounts, row: InteractionRow, n: int) -> None:
    """Add a row's attributes to the main evidence tree, weighted by `n`."""
    for cat in row.categories:
        _increment(counts.categories, cat, n)
    _add_tags(counts.tags, row, n)
    _add_location(counts.location, row, n)


def _apply_rejected(counts: SignalCounts, row: InteractionRow, n: int) -> None:
    """Add a row's attributes to the rejected branch, weighted by `n`."""
    for cat in row.categories:
        _increment(counts.rejected.categories, cat, n)
    _add_tags(counts.rejected.tags, row, n)
    _add_location(counts.rejected.location, row, n)


# ---------------------------------------------------------------------------
# Main aggregation function
# ---------------------------------------------------------------------------


def aggregate_signal_counts(
    rows: list[InteractionRow], weights: Mapping[str, int] | None = None
) -> SignalCounts:
    """Pure function: aggregate interaction rows into SignalCounts. No I/O.

    `weights` maps each lever to the evidence units it contributes. Totals stay
    raw event counts and `source` stays one unit per save; only the category/
    tag/location tree is weighted. See the module docstring for the conviction
    ladder governing saved places via their pill snapshot.
    """
    weights = weights or {}
    counts = SignalCounts()
    seen_evidence: set[str] = set()

    # Process `saved_recommendation` before `save` so the once-per-place
    # dedup keeps the stronger origin's base weight (stable otherwise).
    ordered = sorted(rows, key=lambda r: 0 if r.type == "saved_recommendation" else 1)

    for row in ordered:
        # --- Totals (raw event counts, one per interaction row) ---
        match row.type:
            case "save":
                counts.totals.saves += 1
            case "accepted":
                counts.totals.accepted += 1
            case "saved_recommendation":
                counts.totals.saved_recommendations += 1
            case "rejected":
                counts.totals.rejected += 1

        if row.type in _SAVE_TYPES:
            # Source is link-share-save only (kebi is not a discovery channel),
            # unweighted — one save, one source observation. Counted regardless
            # of curation/sentiment.
            if row.type == "save":
                _increment(counts.source, row.source)

            # Curation gate: a needs-review place is not evidence at all.
            if not row.approved:
                continue
            # One evidence emission per saved place — a place may carry both a
            # link-share `save` and a later `saved_recommendation` row.
            if row.place_core_id is not None:
                if row.place_core_id in seen_evidence:
                    continue
                seen_evidence.add(row.place_core_id)

            # A disliked place is a negative and overrides any positive base.
            if row.liked is False:
                _apply_rejected(counts, row, weights.get("liked_negative", 3))
                continue

            weight = weights.get(row.type, 0)
            if row.visited:
                weight += weights.get("visited", 0)
            if row.liked is True:
                weight += weights.get("liked", 0)
            if weight > 0:
                _apply_positive(counts, row, weight)

        elif row.type == "accepted":
            _apply_positive(counts, row, weights.get("accepted", 1))

        elif row.type == "rejected":
            _apply_rejected(counts, row, weights.get("rejected", 1))

    return counts
