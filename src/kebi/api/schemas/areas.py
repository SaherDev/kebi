"""Response DTOs for GET /v1/areas/{id} (ADR-105, ADR-153).

Explicit shapes: a field leaves the service only by being declared here.
`key` is the raw geo key (what the chat entity payload also carries);
`uri`/`id` values are what a client hands back to a link handler or a
follow-up request, so every row the screen draws is itself tappable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kebi.core.agent.entity_links import area_uri, venue_uri
from kebi.core.areas.models import AreaScreen


class AreaChipDTO(BaseModel):
    """One "best for" chip."""

    icon: str | None = None
    text: str


class AreaBreadcrumbDTO(BaseModel):
    """One tappable ancestor: `indonesia › bali` above the Canggu header."""

    key: str
    name: str
    uri: str


class AreaSubAreaDTO(BaseModel):
    """A child-area row — `saved_count` is the caller's own saves there,
    0 on a "worth knowing" row."""

    key: str
    name: str
    uri: str
    icon: str | None = None
    hook: str | None = None
    saved_count: int = 0


class AreaVenueDTO(BaseModel):
    """A saved-venue row. `subtitle` is server-composed from catalog data;
    `liked`/`visited` are the caller's own pill state for the row's accents."""

    id: str
    name: str
    uri: str
    icon: str | None = None
    subtitle: str | None = None
    liked: bool | None = None
    visited: bool = False


class AreaSectionDTO(BaseModel):
    """The one body section below the profile.

    `saved` — the caller's footprint here: child areas at wide levels,
    venues at the leaf (both may appear when a save carries no deeper geo
    than this level). `worth_knowing` — the profiler's notable children,
    shown only when the caller has no saves under the key.
    """

    kind: Literal["saved", "worth_knowing"]
    areas: list[AreaSubAreaDTO] = Field(default_factory=list)
    places: list[AreaVenueDTO] = Field(default_factory=list)


class AreaScreenResponse(BaseModel):
    """One area as the client renders it (ADR-153).

    `profiled=False` means the global half is still being generated (the
    open that got this response triggered it): `level`/`icon`/`summary` are
    null, `best_for` empty, and the name/breadcrumb are slug-derived
    fallbacks. The personal fields are always live. `section` is null when
    there is nothing to show below the profile.
    """

    key: str
    uri: str
    name: str
    level: str | None = None
    icon: str | None = None
    summary: str | None = None
    best_for: list[AreaChipDTO] = Field(default_factory=list)
    breadcrumb: list[AreaBreadcrumbDTO] = Field(default_factory=list)
    saved_count: int = 0
    profiled: bool = False
    section: AreaSectionDTO | None = None

    @classmethod
    def from_screen(cls, screen: AreaScreen) -> AreaScreenResponse:
        section: AreaSectionDTO | None = None
        if screen.section_kind is not None:
            section = AreaSectionDTO(
                kind=screen.section_kind,
                areas=[
                    AreaSubAreaDTO(
                        key=sub.geo_key,
                        name=sub.name,
                        uri=area_uri(sub.geo_key),
                        icon=sub.icon,
                        hook=sub.hook,
                        saved_count=sub.saved_count,
                    )
                    for sub in screen.sub_areas
                ],
                places=[
                    AreaVenueDTO(
                        id=venue.place_id,
                        name=venue.name,
                        uri=venue_uri(venue.place_id),
                        icon=venue.icon,
                        subtitle=venue.subtitle,
                        liked=venue.liked,
                        visited=venue.visited,
                    )
                    for venue in screen.venues
                ],
            )
        return cls(
            key=screen.geo_key,
            uri=area_uri(screen.geo_key),
            name=screen.name,
            level=screen.level,
            icon=screen.icon,
            summary=screen.summary,
            best_for=[
                AreaChipDTO(icon=chip.icon, text=chip.text) for chip in screen.best_for
            ],
            breadcrumb=[
                AreaBreadcrumbDTO(
                    key=item.geo_key,
                    name=item.name,
                    uri=area_uri(item.geo_key),
                )
                for item in screen.breadcrumb
            ],
            saved_count=screen.saved_count,
            profiled=screen.profiled,
            section=section,
        )
