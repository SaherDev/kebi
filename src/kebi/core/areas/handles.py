"""AreaHandle — the routable identity of the area a place sits in (ADR-165).

A saved place knows where it is, but "Canggu" as a display string is a label,
not a destination. A handle is the same area expressed as something a client
can act on: the canonical key, the name to show, the `kebi://area/...` link
its screen already answers, and the icon its row carries.

One builder mints every handle the API emits — a library row's, a place
screen's, and each entry in the library's area list — so a heading and the
rows under it can never disagree about an area's name. The URI is composed
here and only here: `keys.encode_area_id` is the sole authority on the wire
format, and a client that rebuilt it from the key would be reimplementing a
codec it cannot see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from .keys import display_from_slug, encode_area_id, parent_keys
from .models import AreaProfile

if TYPE_CHECKING:
    from kebi.db.repositories.area_repository import AreaRepository


class AreaRef(BaseModel):
    """An area as something to open: identity, label, destination."""

    key: str
    name: str
    uri: str
    icon: str | None = None


class AreaHandle(AreaRef):
    """An area with its parent, so a client can roll a level up.

    `parent` exists because most-specific keys fragment: a city where some
    places resolve to a neighbourhood and others don't would otherwise render
    neighbourhood headings beside a bare city heading meaning "somewhere else
    in this city". The parent is what lets that be collapsed. `None` at city
    level, whose parent is a country — not an area anyone navigates to.
    """

    parent: AreaRef | None = None


def _name_for(geo_key: str, profiles: dict[str, AreaProfile]) -> str:
    """The area's display name: its profile's, or one derived from the key.

    Area rows are written lazily on first open of an area screen, so most
    keys have no profile yet and the slug is all there is. `kuta-utara` →
    "Kuta Utara" reads correctly; the profiler replaces it with a better name
    the first time anyone opens that area.
    """
    profile = profiles.get(geo_key)
    if profile is not None:
        return profile.name
    return display_from_slug(geo_key.rsplit("/", 1)[-1])


def _ref(geo_key: str, profiles: dict[str, AreaProfile]) -> AreaRef:
    return AreaRef(
        key=geo_key,
        name=_name_for(geo_key, profiles),
        uri=f"kebi://area/{encode_area_id(geo_key)}",
        icon=profiles[geo_key].icon if geo_key in profiles else None,
    )


def _parent_key(geo_key: str) -> str | None:
    """The area one level up, or None when that would be a country."""
    ancestors = parent_keys(geo_key)
    parent = ancestors[-1] if ancestors else None
    return parent if parent and "/" in parent else None


class AreaHandleBuilder:
    """Builds handles for a set of keys in one round trip.

    Batched because the alternative is a lookup per row: a library page of 50
    saves spans a handful of areas, and their names and icons are one indexed
    read for all of them. Parents are fetched in the same read, since a
    client that rolls up needs the parent's name too.
    """

    def __init__(self, area_repo: AreaRepository) -> None:
        self._area_repo = area_repo

    async def for_keys(self, geo_keys: list[str]) -> dict[str, AreaHandle]:
        wanted = {k for k in geo_keys if k}
        if not wanted:
            return {}

        with_parents = set(wanted)
        for key in wanted:
            parent = _parent_key(key)
            if parent:
                with_parents.add(parent)

        profiles = await self._area_repo.get_many(sorted(with_parents))

        handles: dict[str, AreaHandle] = {}
        for key in wanted:
            parent = _parent_key(key)
            handles[key] = AreaHandle(
                **_ref(key, profiles).model_dump(),
                parent=_ref(parent, profiles) if parent else None,
            )
        return handles
