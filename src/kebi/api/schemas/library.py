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

from kebi.core.knowledge.schemas import PlaceNote, note_source_label
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
    """Body for POST /v1/user/places — the plain "save this place" action.

    `place_core_id` is the catalog id off the place's `kebi://venue/{id}`
    link; the place already exists in the catalog (kebi surfaced it), so the
    save just links it to the caller. No turn context rides along — the
    recommendation card and its `recommendation_id`/`reason` ceremony are gone
    (ADR-151); reaching this endpoint at all is what marks the save as
    kebi-recommended (the route stamps `PlaceSource.kebi` and emits the
    strong taste signal). `user_id` is intentionally absent (gateway
    identity, ADR-105). `extra="forbid"` rejects unknown keys with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    place_core_id: str = Field(
        ..., description="places.id of the place to save (the venue link's key)"
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


class PlaceNoteView(BaseModel):
    """One insider note on a saved place — the public projection of a knowledge
    claim (ADR-127, ADR-105).

    `id` is the underlying claim's id — a stable list key and the target a
    future agree/disagree vote will address. `agree_count`/`disagree_count` are
    its corroboration tally (both 0 until voting ships). `source` is a coarse
    origin label (`community` / `expert` / `kebi`), not the raw `source_type`.
    `from_shared` marks a note mined from the very post the user shared for this
    save, so the client can badge it without any grouping. Raw
    `source_ref`/`confidence` stay unexposed.
    """

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str
    from_shared: bool
    agree_count: int
    disagree_count: int

    @classmethod
    def from_note(cls, note: PlaceNote) -> PlaceNoteView:
        return cls(
            id=note.id,
            text=note.text,
            tags=list(note.tags),
            source=note_source_label(note.source_type),
            from_shared=note.from_shared,
            agree_count=note.agree_count,
            disagree_count=note.disagree_count,
        )


class LibraryItem(BaseModel):
    """One place as the client renders it: the catalog place, the caller's
    relationship to it, and the insider notes tied to it (ADR-127; empty when
    it has none).

    Doubles as the place-screen payload (`GET /v1/places/{place_id}`,
    ADR-151): there `user_data` is null when the caller never saved the
    place — same shape either way, so a venue tap and a library row open
    the identical screen. On a Library page it is always present (every row
    *is* a save)."""

    place: PlaceCore
    user_data: LibraryUserData | None
    claims: list[PlaceNoteView] = Field(default_factory=list)

    @classmethod
    def from_view(
        cls, view: SavedPlaceView, notes: list[PlaceNote] | None = None
    ) -> LibraryItem:
        return cls(
            place=view.place,
            user_data=LibraryUserData.from_user_place(view.user_data),
            claims=[PlaceNoteView.from_note(n) for n in (notes or [])],
        )

    @classmethod
    def from_place(
        cls,
        place: PlaceCore,
        user_place: UserPlace | None,
        notes: list[PlaceNote] | None = None,
    ) -> LibraryItem:
        """The place-screen shape: a catalog place with or without a save."""
        return cls(
            place=place,
            user_data=(
                LibraryUserData.from_user_place(user_place) if user_place else None
            ),
            claims=[PlaceNoteView.from_note(n) for n in (notes or [])],
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
        cls,
        views: list[SavedPlaceView],
        next_cursor: str | None,
        total: int,
        notes_by_place: dict[str, list[PlaceNote]] | None = None,
    ) -> LibraryResponse:
        notes = notes_by_place or {}
        return cls(
            places=[
                LibraryItem.from_view(v, notes.get(v.place.id) if v.place.id else None)
                for v in views
            ],
            next_cursor=next_cursor,
            total=total,
        )
