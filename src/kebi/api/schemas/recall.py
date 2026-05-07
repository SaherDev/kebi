"""Request/response schemas for the recall endpoint (feature 019 shape).

The HTTP response now carries `PlaceObject` directly. Each result in the
list wraps a place with its `match_reason` (`"filter"`, `"semantic"`,
`"keyword"`, or `"semantic + keyword"`) and an optional `relevance_score`
(populated only in hybrid mode).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kebi.core.places.models import PlaceObject


class RecallRequest(BaseModel):
    """Recall request from NestJS."""

    query: str | None = Field(
        default=None,
        description="Natural language query (None → filter mode)",
    )
    user_id: str = Field(..., description="User identifier (injected by NestJS)")


class RecallResult(BaseModel):
    """A single recall result — a `PlaceObject` annotated with match metadata."""

    place: PlaceObject
    match_reason: str = Field(
        description='"filter" | "semantic" | "keyword" | "semantic + keyword"'
    )
    relevance_score: float | None = Field(
        default=None,
        description=(
            "Score scale depends on score_type — rrf scores are typically "
            "0.01–0.03, ts_rank scores are 0–1. Never compare across types."
        ),
    )
    score_type: Literal["rrf", "ts_rank"] | None = None


class RecallResponse(BaseModel):
    """Recall response to NestJS."""

    results: list[RecallResult]
    total_count: int = Field(
        description=(
            "Number of places matching the filter/query before LIMIT. "
            "Post-distance-filter this is best-effort (see recall service docstring)."
        )
    )
    empty_state: bool = Field(
        default=False, description="True only when user has zero saved places"
    )
