"""Input/output types for the recall pipeline (feature 019, feature 028 M4).

`RecallFilters` is a Pydantic `BaseModel` extending the shared
`PlaceFilters` base (`core/places/filters.py`) per ADR-056. The
migration from `@dataclass` to Pydantic landed in feature 028 M4 — the
shared base plus the three retrieval-specific extensions
(`max_distance_km`, `created_after`, `created_before`) live here. Every
top-level `PlaceObject` key is reachable through the inherited base;
attribute-level signals (cuisine, price_hint, ambiance, dietary,
good_for, location_context) nest under `attributes: PlaceAttributes`.

`RecallResult` wraps a `PlaceObject` with the `match_reason` string and
optional `relevance_score` (populated only in hybrid mode).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from kebi.core.places.filters import PlaceFilters
from kebi.core.places.models import PlaceObject


class RecallFilters(PlaceFilters):
    """Retrieval filters mirroring `PlaceObject` (ADR-056) + retrieval-only fields.

    Inherits `place_type`, `subcategory`, `tags_include`, `attributes`,
    `source` from `PlaceFilters`. Adds `max_distance_km`,
    `created_after`, `created_before` for the recall pipeline.
    """

    max_distance_km: float | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


@dataclass
class RecallResult:
    place: PlaceObject
    match_reason: str
    relevance_score: float | None = None
    """Score scale depends on score_type — rrf scores are typically 0.01–0.03,
    ts_rank scores are 0–1. Never compare across types."""

    score_type: Literal["rrf", "ts_rank"] | None = None


__all__ = ["RecallFilters", "RecallResult"]
