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

from pydantic import BaseModel, Field

from .handles import AreaHandle, AreaHandleBuilder

if TYPE_CHECKING:
    from kebi.core.places.protocols import UserPlacesRepoProtocol


class AreaWithCount(BaseModel):
    """One area the user has saves in, and how many."""

    area: AreaHandle
    count: int


class LibraryAreaIndex(BaseModel):
    """The whole index: every area with its count, plus the keyless remainder.

    `unassigned` is not an area and never becomes one — it has no key, no
    name and no screen. It is here because it is the one heading the client
    would otherwise have to count for itself, and a library that is only
    partly paged makes that arithmetic wrong on screen. `areas` counts plus
    `unassigned` equal the user's library total.
    """

    areas: list[AreaWithCount] = Field(default_factory=list)
    unassigned: int = 0


class LibraryAreasService:
    def __init__(
        self,
        user_places_repo: UserPlacesRepoProtocol,
        handle_builder: AreaHandleBuilder,
    ) -> None:
        self._user_places_repo = user_places_repo
        self._handles = handle_builder

    async def list_areas(self, user_id: str) -> LibraryAreaIndex:
        """Every area holding at least one of the caller's saves, plus how
        many saves hold no area at all.

        Two reads regardless of library size: one grouped count over the
        user's saves, then one batched lookup for the names and icons of the
        areas that came back (and their parents). An area with no profile row
        still appears — its screen renders unprofiled too, so a heading for it
        is a working destination, not a dead one.

        A key the builder cannot mint a handle for is dropped from `areas`
        and folded into `unassigned` rather than vanishing: the two numbers
        are only useful if every save is in exactly one of them.
        """
        distribution = await self._user_places_repo.area_distribution(user_id)
        if not distribution.areas:
            return LibraryAreaIndex(unassigned=distribution.unassigned)

        handles = await self._handles.for_keys([key for key, _ in distribution.areas])
        unhandled = sum(
            count for key, count in distribution.areas if key not in handles
        )
        return LibraryAreaIndex(
            areas=[
                AreaWithCount(area=handles[key], count=count)
                for key, count in distribution.areas
                if key in handles
            ],
            unassigned=distribution.unassigned + unhandled,
        )
