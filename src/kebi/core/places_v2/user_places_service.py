"""UserPlacesService — combines user_places + places repos."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from .models import PlaceCore, PlaceSource, SavedPlaceView, UserPlace
from .protocols import (
    PlacesRepoProtocol,
    UserPlacesRepoProtocol,
)

logger = logging.getLogger(__name__)


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
        places_repo: PlacesRepoProtocol,
        user_places_repo: UserPlacesRepoProtocol,
    ) -> None:
        self._places_repo = places_repo
        self._user_places_repo = user_places_repo

    async def save_places(
        self,
        user_id: str,
        places: list[PlaceCore],
        source: PlaceSource,
        source_url: str | None,
    ) -> list[UserPlace]:
        """Link `places` to `user_id` in user_places.

        Caller must have already persisted each `PlaceCore` via
        `PlaceUpsertService.upsert_and_embed` so every core carries an `id`.
        Cores without an `id` are rejected — there's nothing to link to.

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

        now = datetime.now(UTC)
        rows = [
            UserPlace(
                user_place_id=str(uuid.uuid4()),
                user_id=user_id,
                place_id=pid,
                approved=False,
                source=source,
                source_url=source_url,
                saved_at=now,
            )
            for pid in place_ids
        ]
        return await self._user_places_repo.save_user_places(rows)

    async def get_user_places(self, user_id: str) -> list[SavedPlaceView]:
        """Two reads: user_places → places. Zero writes."""
        user_places = await self._user_places_repo.get_by_user(user_id)
        if not user_places:
            return []

        place_ids = [up.place_id for up in user_places]
        cores = await self._places_repo.get_by_ids(place_ids)
        cores_by_id = {c.id: c for c in cores if c.id}

        result: list[SavedPlaceView] = []
        for up in user_places:
            core = cores_by_id.get(up.place_id)
            if core is None:
                logger.warning(
                    "user_place_missing_core",
                    extra={"place_id": up.place_id, "user_id": user_id},
                )
                continue
            result.append(SavedPlaceView(place=core, user_data=up))

        return result

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
