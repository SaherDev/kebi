"""Domain models for the area surface (ADR-153).

An `AreaProfile` is the global half of an area screen — one row per geo
key, generated once by the profiler and identical for every user. The
personal half (where the caller has saved) is computed per request and
never stored here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Display label for the key's scope. Deliberately richer than the key's
# three structural positions: Bali sits in the key's city slot but no one
# calls it a city, so the profiler labels what the place *is* and the key
# keeps encoding where it sits.
AreaLevel = Literal["country", "region", "island", "city", "district", "neighbourhood"]


class AreaChip(BaseModel):
    """One "best for" chip: an optional emoji and a short lowercase label."""

    icon: str | None = None
    text: str


class NotableSubArea(BaseModel):
    """A child area the profiler considers worth knowing.

    `geo_key` is built mechanically from the parent key plus the child's
    name — the LLM emits names only and can never invent a key, the same
    rule the chat linkifier follows.
    """

    geo_key: str
    name: str
    icon: str | None = None
    # One-line hook ("surf & laptops"), lowercase, a few words.
    hook: str | None = None


class AreaProfile(BaseModel):
    """The stored, user-independent profile of one area."""

    geo_key: str
    name: str
    level: AreaLevel
    icon: str | None = None
    summary: str
    best_for: list[AreaChip] = Field(default_factory=list)
    # Ancestor display names, outermost first ("Indonesia", "Bali") — what
    # the breadcrumb renders. Parallel to `parent_keys(geo_key)`.
    breadcrumb: list[str] = Field(default_factory=list)
    notable_sub_areas: list[NotableSubArea] = Field(default_factory=list)
    profiled_at: datetime | None = None


class BreadcrumbItem(BaseModel):
    """One tappable ancestor in the screen's breadcrumb."""

    geo_key: str
    name: str


class SectionArea(BaseModel):
    """A child-area row in the screen's body section."""

    geo_key: str
    name: str
    icon: str | None = None
    hook: str | None = None
    saved_count: int = 0


class SectionVenue(BaseModel):
    """A venue row in the screen's body section (leaf level, or a save that
    sits directly at a wide level because its place carries no deeper geo)."""

    place_id: str
    name: str
    icon: str | None = None
    subtitle: str | None = None
    liked: bool | None = None
    visited: bool = False


SectionKind = Literal["saved", "worth_knowing"]


class AreaScreen(BaseModel):
    """Everything one `GET /v1/areas/{id}` response says, pre-DTO.

    The global half comes off the `AreaProfile` row (None-ish fields until
    the first dressing lands); the personal half — `saved_count` and the
    `saved` section — is computed fresh per caller. `section_kind` is None
    when there is nothing to show below the profile: no saves here and no
    notable children to fall back to.
    """

    geo_key: str
    name: str
    level: AreaLevel | None = None
    icon: str | None = None
    summary: str | None = None
    best_for: list[AreaChip] = Field(default_factory=list)
    breadcrumb: list[BreadcrumbItem] = Field(default_factory=list)
    saved_count: int = 0
    profiled: bool = False
    section_kind: SectionKind | None = None
    sub_areas: list[SectionArea] = Field(default_factory=list)
    venues: list[SectionVenue] = Field(default_factory=list)
