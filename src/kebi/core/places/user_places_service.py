"""UserPlacesService — write + read operations over a user's saved places."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from ._cursor import LibraryCursor
from .models import (
    PlaceCore,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
)
from .protocols import UserPlacesRepoProtocol


class DuplicateUserPlaceError(Exception):
    """Raised by save_places when one or more candidates are already in the
    user's saved list. The whole batch is rolled back — no writes happen.
    """

    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = conflicts
        super().__init__(
            f"User already has {len(conflicts)} place(s) saved: {conflicts}"
        )


class UserPlacesService:
    def __init__(
        self,
        user_places_repo: UserPlacesRepoProtocol,
    ) -> None:
        self._user_places_repo = user_places_repo

    async def save_places(
        self,
        user_id: str,
        places: list[PlaceCore],
        source: PlaceSource,
        source_ref: str | None,
        source_labels: Mapping[str, str | None] | None = None,
    ) -> list[UserPlace]:
        """Link `places` to `user_id` in user_places.

        Caller must have already persisted each `PlaceCore` via
        `PlaceUpsertService.upsert_and_embed` so every core carries an `id`.
        Cores without an `id` are rejected — there's nothing to link to.

        `source_labels` maps `place.id` → the name the place was shown as
        in the source post when it differs from the canonical name (the
        product shows the user the name they know it by). Absent / None
        entries store NULL — the default keeps existing callers and the
        cache-hit path unchanged.

        Duplicate policy: if any incoming `place_id` is already in the
        user's saved list, raises `DuplicateUserPlaceError` and writes
        nothing. Matches the legacy "rollback the whole batch" semantics.
        """
        if not places:
            return []

        place_ids: list[str] = []
        for p in places:
            if p.id is None:
                raise ValueError(
                    f"PlaceCore {p.place_name!r} has no id — call "
                    "PlaceUpsertService.upsert_and_embed first"
                )
            place_ids.append(p.id)

        existing_ids = await self._user_places_repo.get_existing_place_ids(
            user_id, place_ids
        )
        if existing_ids:
            conflicts = [pid for pid in place_ids if pid in existing_ids]
            raise DuplicateUserPlaceError(conflicts=conflicts)

        labels = source_labels or {}
        now = datetime.now(UTC)
        rows = [
            UserPlace(
                user_place_id=str(uuid.uuid4()),
                user_id=user_id,
                place_id=pid,
                approved=False,
                source=source,
                source_ref=source_ref,
                source_label=labels.get(pid),
                saved_at=now,
            )
            for pid in place_ids
        ]
        return await self._user_places_repo.save_user_places(rows)

    async def browse(
        self,
        user_id: str,
        filters: SavedPlaceFilters,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[SavedPlaceView], str | None]:
        """One filtered, keyset-paged page of the user's saved places.

        The opaque `cursor` token is owned end-to-end here: this is the only
        boundary that decodes the incoming token and encodes the outgoing
        `next_cursor` (see `LibraryCursor`); callers pass and receive opaque
        strings. A malformed token raises `ValueError`.

        Single read — the repo JOINs `user_places ⋈ places` and returns the
        combined `SavedPlaceView` directly; a saved place whose catalog row is
        missing is simply excluded by the inner join. Fetches `limit + 1` to
        detect a next page without a separate count: more than `limit` rows
        means another page, whose cursor is the last kept row's anchor;
        otherwise the cursor is None and the client stops paging.
        """
        anchor = LibraryCursor.decode(cursor) if cursor else None
        rows = await self._user_places_repo.browse(
            user_id, filters, limit=limit + 1, cursor=anchor
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            LibraryCursor.from_view(page[-1]).encode() if has_more and page else None
        )
        return page, next_cursor

    async def update_status(
        self,
        user_place_id: str,
        *,
        visited: bool | None = None,
        liked: bool | None = None,
        approved: bool | None = None,
        note: str | None = None,
    ) -> UserPlace:
        """Mutate status flags and note. Returns updated UserPlace."""
        existing = await self._user_places_repo.get_by_user_place_id(user_place_id)
        if existing is None:
            raise ValueError(f"user_place_id not found: {user_place_id}")

        updates = {
            k: v
            for k, v in {
                "visited": visited,
                "liked": liked,
                "approved": approved,
                "note": note,
            }.items()
            if v is not None
        }
        updated = existing.model_copy(update=updates)
        saved = await self._user_places_repo.save_user_places([updated])
        return saved[0]
