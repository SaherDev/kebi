"""UserPlacesService — write + read operations over a user's saved places."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from ._cursor import LibraryCursor
from .models import (
    LibrarySort,
    PlaceCore,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
    UserPlaceStatusUpdate,
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
        sort: LibrarySort = LibrarySort.recent,
    ) -> tuple[list[SavedPlaceView], str | None]:
        """One filtered, keyset-paged page of the user's saved places.

        `sort` selects the order (recent ↔ A–Z) and is carried into the keyset
        anchor: the opaque `cursor` token is owned end-to-end here — this is
        the only boundary that decodes the incoming token and encodes the
        outgoing `next_cursor` (see `LibraryCursor`); callers pass and receive
        opaque strings. A malformed token, or one minted under a different
        sort, raises `ValueError`.

        Single read — the repo JOINs `user_places ⋈ places` and returns the
        combined `SavedPlaceView` directly; a saved place whose catalog row is
        missing is simply excluded by the inner join. Fetches `limit + 1` to
        detect a next page without a separate count: more than `limit` rows
        means another page, whose cursor is the last kept row's anchor;
        otherwise the cursor is None and the client stops paging.
        """
        anchor = LibraryCursor.decode(cursor) if cursor else None
        rows = await self._user_places_repo.browse(
            user_id, filters, limit=limit + 1, cursor=anchor, sort=sort
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            LibraryCursor.from_view(page[-1], sort).encode()
            if has_more and page
            else None
        )
        return page, next_cursor

    async def update_status(
        self, user_place_id: str, user_id: str, changes: UserPlaceStatusUpdate
    ) -> UserPlace | None:
        """Apply a partial status update to a save the caller owns.

        Only the fields the caller actually set are written — an unset field
        is left untouched, an explicit `None` clears it (un-like, clear a
        note).

        Returns the updated `UserPlace`, or `None` when nothing matched — the
        save does not exist or belongs to another user (the repo scopes the
        update on `user_id`, so the two are indistinguishable by design). No
        raise: the route maps `None` to 404.
        """
        return await self._user_places_repo.update_fields(
            user_place_id, user_id, changes
        )

    async def delete_place(self, user_place_id: str, user_id: str) -> bool:
        """Remove a single saved place the caller owns.

        Idempotent and ownership-scoped: returns True if a row was deleted,
        False if nothing matched — either the save does not exist or it
        belongs to another user (the repo scopes the delete on `user_id`,
        so the two are indistinguishable by design). No existence check
        and no raise: the route maps both outcomes to 204.
        """
        deleted = await self._user_places_repo.delete_one(user_place_id, user_id)
        return deleted > 0
