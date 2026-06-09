"""Request/response schemas for GET /v1/user/library.

The Library screen browses a user's saved places (`user_places ⋈ places`)
with optional filters and keyset ("cursor") pagination. Filters + paging are
carried as a single Pydantic query-params model (`LibraryQuery`); the response
is a page of `SavedPlaceView` plus an opaque `next_cursor`.

The cursor is opaque on the wire here — its encoding lives in one place,
`UserPlacesService`/`LibraryCursor`. These schemas just pass the token through.

`user_id` is intentionally absent from every shape here — the caller's
identity arrives via the gateway header `X-Gateway-User-Id` and is verified by
`require_gateway_identity`. A caller can only ever read their own library.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.places import (
    PlaceCategory,
    PlaceCore,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
)


class LibraryQuery(BaseModel):
    """Query params for the Library browse endpoint.

    Repeatable filters (`category`, `tag`) accept the param multiple times
    (`?category=cafe&category=bar`). `extra="forbid"` rejects unknown query
    params with a 422 rather than silently ignoring a typo'd filter.
    """

    model_config = ConfigDict(extra="forbid")

    # ---- filters (mapped to SavedPlaceFilters) ----
    category: list[PlaceCategory] | None = Field(
        None, description="Filter by place category (OR across repeats)."
    )
    tag: list[str] | None = Field(
        None, description="Filter by tag value (AND across repeats)."
    )
    city: str | None = Field(None, description="Filter by city (case-insensitive).")
    country: str | None = Field(None, description="Filter by country code (exact).")
    source: PlaceSource | None = Field(
        None, description="Filter by where the place was saved from."
    )
    visited: bool | None = Field(None, description="Filter by visited flag.")
    liked: bool | None = Field(None, description="Filter by liked flag.")
    approved: bool | None = Field(None, description="Filter by curation flag.")
    saved_after: datetime | None = Field(
        None, description="Only saves on/after this ISO-8601 instant."
    )
    saved_before: datetime | None = Field(
        None, description="Only saves on/before this ISO-8601 instant."
    )

    # ---- paging ----
    limit: int = Field(50, ge=1, le=100, description="Max places per page.")
    cursor: str | None = Field(
        None,
        description=(
            "Opaque pagination cursor from a prior response's `next_cursor`. "
            "Omit for the first page."
        ),
    )

    def to_filters(self) -> SavedPlaceFilters:
        """Map the filter params to the domain filter (paging excluded)."""
        return SavedPlaceFilters(
            categories=self.category,
            tags=self.tag,
            city=self.city,
            country=self.country,
            source=self.source,
            visited=self.visited,
            liked=self.liked,
            approved=self.approved,
            saved_after=self.saved_after,
            saved_before=self.saved_before,
        )


class LibraryUserData(BaseModel):
    """The caller's relationship to a saved place — safe public projection.

    Mirrors `UserPlace` **minus `user_id`**: echoing the caller's identity
    back in the payload is unnecessary (the client authenticated as that
    user) and is deliberately excluded. The API never returns the raw
    domain model, so a field added to `UserPlace` is not leaked by default —
    it must be added here intentionally.
    """

    user_place_id: str
    place_id: str
    approved: bool
    visited: bool
    liked: bool | None
    note: str | None
    source: PlaceSource
    source_ref: str | None
    source_label: str | None
    saved_at: datetime
    visited_at: datetime | None


class LibraryItem(BaseModel):
    """One saved place on a Library page: the catalog place + the user's data."""

    place: PlaceCore
    user_data: LibraryUserData

    @classmethod
    def from_view(cls, view: SavedPlaceView) -> LibraryItem:
        # model_validate drops UserPlace.user_id — LibraryUserData doesn't
        # declare it, so the extra key is ignored.
        return cls(
            place=view.place,
            user_data=LibraryUserData.model_validate(
                view.user_data, from_attributes=True
            ),
        )


class LibraryResponse(BaseModel):
    """One page of the user's saved places."""

    places: list[LibraryItem] = Field(
        default_factory=list, description="The saved places on this page."
    )
    next_cursor: str | None = Field(
        None,
        description=(
            "Opaque cursor for the next page, or null when this is the last "
            "page. An empty library returns an empty list with a null cursor."
        ),
    )

    @classmethod
    def from_page(
        cls, views: list[SavedPlaceView], next_cursor: str | None
    ) -> LibraryResponse:
        return cls(
            places=[LibraryItem.from_view(v) for v in views],
            next_cursor=next_cursor,
        )
