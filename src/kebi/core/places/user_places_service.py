"""UserPlacesService — write + read operations over a user's saved places."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

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


class PlaceNotFoundError(Exception):
    """Raised by save_one when place_id has no row in the catalog.

    The insert trips the `user_places.place_id → places.id` foreign key —
    the catalog is the source of truth, so the route maps this to a 404
    rather than letting an IntegrityError surface as a 500.
    """

    def __init__(self, place_id: str) -> None:
        self.place_id = place_id
        super().__init__(f"No catalog place for id {place_id!r}")


class SaveLimitExceededError(Exception):
    """Raised by the save path when a write would exceed the user's plan cap.

    The cap is a plan-tier entitlement the gateway passes per request
    (`save_limit`); kebi enforces it here because only kebi can count the
    user's saves (they live in this repo's DB; NestJS never touches it —
    Constitution §V). This is the single chokepoint for both write
    endpoints (`POST /v1/extract` and `POST /v1/user/places`), so neither
    can bypass the cap.
    """

    def __init__(self, user_id: str, current: int, limit: int) -> None:
        self.current = current
        self.limit = limit
        super().__init__(f"User {user_id!r} at save limit: {current}/{limit}")


class UserPlacesService:
    def __init__(
        self,
        user_places_repo: UserPlacesRepoProtocol,
    ) -> None:
        self._user_places_repo = user_places_repo

    async def count_saves(self, user_id: str) -> int:
        """Total saves the user holds — the plan-cap counter and library hero
        count both read this single source.
        """
        return await self._user_places_repo.count_by_user(user_id)

    async def _enforce_save_limit(
        self, user_id: str, save_limit: int | None, incoming: int
    ) -> None:
        """Raise `SaveLimitExceededError` if saving `incoming` rows would push
        the user past `save_limit`. No-op when `save_limit is None` (unlimited).
        """
        if save_limit is None:
            return
        current = await self._user_places_repo.count_by_user(user_id)
        if current + incoming > save_limit:
            raise SaveLimitExceededError(user_id, current, save_limit)

    async def save_places(
        self,
        user_id: str,
        places: list[PlaceCore],
        source: PlaceSource,
        source_ref: str | None,
        source_labels: Mapping[str, str | None] | None = None,
        save_limit: int | None = None,
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

        # Plan-cap check after dedup — every remaining id is a genuinely new
        # save, so it counts against the limit.
        await self._enforce_save_limit(user_id, save_limit, len(place_ids))

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

    async def save_one(
        self,
        user_id: str,
        place_id: str,
        source: PlaceSource,
        save_limit: int | None = None,
    ) -> tuple[UserPlace, bool]:
        """Save a single catalog place to the user's library, idempotently.

        Backs the "save it" action on a recommendation card: the place is
        already in the catalog, so this just links it. Returns
        `(row, created)` — `created=False` when the user already holds a save
        for `place_id` (a re-tap returns the existing row instead of raising,
        and the caller skips re-emitting the positive taste signal); otherwise
        inserts one `user_places` row and returns `(row, True)`.

        The save carries no note: the pick's reason is now written to the
        knowledge layer as a `kebi_message` claim by the route (ADR-127), not
        stored here. `user_places.note` is set only by the user's own edit
        (`PATCH /v1/user/places/{id}`).

        Raises `PlaceNotFoundError` when `place_id` is absent from the catalog:
        the insert trips the `place_id → places.id` foreign key, the source of
        truth, so there is no separate existence pre-check.
        """
        existing = await self._user_places_repo.get_by_user_and_place(user_id, place_id)
        if existing is not None:
            return existing, False

        # Only a genuinely new save counts against the cap — a re-tap on an
        # already-saved place returned above without reaching here.
        await self._enforce_save_limit(user_id, save_limit, 1)

        row = UserPlace(
            user_place_id=str(uuid.uuid4()),
            user_id=user_id,
            place_id=place_id,
            # Deliberately saving a place kebi recommended is itself a curation
            # act — it lands approved (not needs-review) so it trains taste
            # immediately (ADR-115), unlike a passive link-share batch save.
            approved=True,
            source=source,
            source_ref=None,
            saved_at=datetime.now(UTC),
        )
        try:
            saved = await self._user_places_repo.save_user_places([row])
        except IntegrityError as exc:
            raise PlaceNotFoundError(place_id) from exc
        return saved[0], True

    async def browse(
        self,
        user_id: str,
        filters: SavedPlaceFilters,
        limit: int,
        cursor: str | None = None,
        sort: LibrarySort = LibrarySort.recent,
    ) -> tuple[list[SavedPlaceView], str | None, int]:
        """One filtered, keyset-paged page of the user's saved places, plus the
        unfiltered grand total of the caller's saves.

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

        `total` is the whole library size — independent of the page's filters
        and cursor — and drives the screen's hero count, so it is the same on
        every page.
        """
        anchor = LibraryCursor.decode(cursor) if cursor else None
        rows = await self._user_places_repo.browse(
            user_id, filters, limit=limit + 1, cursor=anchor, sort=sort
        )
        total = await self._user_places_repo.count_by_user(user_id)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = (
            LibraryCursor.from_view(page[-1], sort).encode()
            if has_more and page
            else None
        )
        return page, next_cursor, total

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
