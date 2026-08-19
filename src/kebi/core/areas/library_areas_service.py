"""The library's area index — which areas a user's saves fall into (ADR-165).

The Library screen groups saves by area, and grouping needs two things the
client cannot compute for itself: the complete set of areas (a page it hasn't
loaded may be the only one holding a given area) and an exact count per area.
Both are properties of the whole library, so both are answered here.

Deliberately *data*, not a screen. No rollup, no pinning, no truncation, and
the ordering carries no meaning — how to lay areas out is the client's call,
and a second client with a different layout must not need a second endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from .handles import AreaHandle, AreaHandleBuilder

if TYPE_CHECKING:
    from kebi.core.places.protocols import UserPlacesRepoProtocol


class AreaWithCount(BaseModel):
    """One area the user has saves in, and how many."""

    area: AreaHandle
    count: int


class LibraryAreasService:
    def __init__(
        self,
        user_places_repo: UserPlacesRepoProtocol,
        handle_builder: AreaHandleBuilder,
    ) -> None:
        self._user_places_repo = user_places_repo
        self._handles = handle_builder

    async def list_areas(self, user_id: str) -> list[AreaWithCount]:
        """Every area holding at least one of the caller's saves.

        Two reads regardless of library size: one grouped count over the
        user's saves, then one batched lookup for the names and icons of the
        areas that came back (and their parents). An area with no profile row
        still appears — its screen renders unprofiled too, so a heading for it
        is a working destination, not a dead one.
        """
        distribution = await self._user_places_repo.area_distribution(user_id)
        if not distribution:
            return []

        handles = await self._handles.for_keys([key for key, _ in distribution])
        return [
            AreaWithCount(area=handles[key], count=count)
            for key, count in distribution
            if key in handles
        ]
