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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kebi.core.places import (
    LibrarySort,
    PlaceCategory,
    PlaceCore,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
    UserPlaceStatusUpdate,
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

    # ---- sort + paging ----
    sort: LibrarySort = Field(
        LibrarySort.recent,
        description=(
            "Order: `recent` (newest-saved first, default) or `name` "
            "(case-insensitive A–Z). A `cursor` must be replayed under the "
            "same sort it was issued for; switching sort restarts paging."
        ),
    )
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


class SaveUserPlaceRequest(BaseModel):
    """Body for POST /v1/user/places — the consult card's "save it" action.

    `place_core_id` is the catalog id of the recommended place; it already
    exists in the catalog (it was just recommended), so the save just links it
    to the caller. `recommendation_id` is the id kebi minted on the consult
    result the place came from — it attributes the `saved_recommendation`
    taste signal back to that recommendation. `source` is not accepted from
    the client — the route stamps `PlaceSource.kebi`. `user_id` is
    intentionally absent (gateway identity, ADR-105). `extra="forbid"` rejects
    unknown keys with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    place_core_id: str = Field(
        ..., description="places.id of the recommended place to save"
    )
    recommendation_id: str = Field(
        ..., description="id of the recommendation the place was saved from"
    )


class UserPlaceStatusPatch(BaseModel):
    """Partial update body for PATCH /v1/user/places/{user_place_id}.

    The pills and menu actions toggle a save's user-state: `visited`
    ("✅ been there"), `liked` ("❤️ i like this one"), `approved`
    ("👍 looks right"), and a free-text `note`. Every field is optional —
    a request carries only what changed.

    Set vs. unset is meaningful and is *not* the same as null: an omitted
    field is left untouched, while an explicit `null` clears the column
    (un-like back to neutral, erase a note). `extra="forbid"` rejects
    unknown keys with a 422, and an empty body (no fields) is rejected too —
    a no-op patch is a client mistake, not a silent success.
    """

    model_config = ConfigDict(extra="forbid")

    visited: bool | None = None
    liked: bool | None = None
    approved: bool | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _reject_empty(self) -> UserPlaceStatusPatch:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self

    def to_update(self) -> UserPlaceStatusUpdate:
        """Map to the domain update, preserving exactly which fields were set.

        Round-tripping through `exclude_unset` keeps the set/unset distinction
        intact, so the service and repo write only the fields the caller sent
        (an explicit `null` survives as a clear).
        """
        return UserPlaceStatusUpdate(**self.model_dump(exclude_unset=True))


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

    @classmethod
    def from_user_place(cls, up: UserPlace) -> LibraryUserData:
        # model_validate drops UserPlace.user_id — this DTO doesn't declare
        # it, so the extra attribute is ignored (ADR-105).
        return cls.model_validate(up, from_attributes=True)


class LibraryItem(BaseModel):
    """One saved place on a Library page: the catalog place + the user's data."""

    place: PlaceCore
    user_data: LibraryUserData

    @classmethod
    def from_view(cls, view: SavedPlaceView) -> LibraryItem:
        return cls(
            place=view.place,
            user_data=LibraryUserData.from_user_place(view.user_data),
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
    total: int = Field(
        ...,
        description=(
            "Grand total of the caller's saves, unfiltered — the full library "
            "size regardless of any page or filter applied to this response."
        ),
    )

    @classmethod
    def from_page(
        cls, views: list[SavedPlaceView], next_cursor: str | None, total: int
    ) -> LibraryResponse:
        return cls(
            places=[LibraryItem.from_view(v) for v in views],
            next_cursor=next_cursor,
            total=total,
        )
