"""UserPlacesService — combines user_places + places repos and live-field enrichment."""

from __future__ import annotations

import logging

from ._place_utils import overlay_with_cache
from .models import SavedPlaceView, UserPlace
from .protocols import (
    PlacesRepoProtocol,
    PlacesSearchServiceProtocol,
    UserPlacesRepoProtocol,
)

logger = logging.getLogger(__name__)


class UserPlacesService:
    def __init__(
        self,
        places_repo: PlacesRepoProtocol,
        user_places_repo: UserPlacesRepoProtocol,
        search: PlacesSearchServiceProtocol,
    ) -> None:
        self._places_repo = places_repo
        self._user_places_repo = user_places_repo
        self._search = search

    async def get_user_places(self, user_id: str) -> list[SavedPlaceView]:
        """Three reads: user_places → places → cache overlay. Zero writes."""
        user_places = await self._user_places_repo.get_by_user(user_id)
        if not user_places:
            return []

        place_ids = [up.place_id for up in user_places]
        cores = await self._places_repo.get_by_ids(place_ids)
        cores_by_id = {c.id: c for c in cores if c.id}

        provider_ids = [c.provider_id for c in cores if c.provider_id]
        fresh = await self._search.get_by_ids(provider_ids) if provider_ids else {}

        place_objects = {
            obj.id: obj
            for obj in overlay_with_cache(list(cores_by_id.values()), fresh)
            if obj.id
        }

        result: list[SavedPlaceView] = []
        for up in user_places:
            place = place_objects.get(up.place_id)
            if place is None:
                logger.warning(
                    "user_place_missing_core",
                    extra={"place_id": up.place_id, "user_id": user_id},
                )
                continue
            result.append(SavedPlaceView(place=place, user_data=up))

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
