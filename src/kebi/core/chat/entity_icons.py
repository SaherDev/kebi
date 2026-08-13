"""Entity icons come off the stored row — nothing per-turn invents one.

A chat chip and the screen its tap opens must show the same icon. They
didn't: a venue chip carried the icon snapshot riding the tool payload
(stale the moment the place screen's first-open profiler picked one,
ADR-152), and an area chip carried the location resolver's per-turn pick
while the area screen shows the profiler's stored pick (ADR-153) — two
different models choosing icons for the same entity, so they disagreed
by construction.

This refresher runs once per answer, after linkify, over the handful of
entities actually linked: one batch read per kind, icons swapped for
whatever the row says right now. An entity whose row has no icon (or no
row yet — an unprofiled area) ships `icon: null` and the client falls
back, which is the same thing its screen will do. Web entities carry a
static glyph and are untouched.

Icons are decoration: any failure here logs and ships the entities
unchanged, never an error.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.agent.entity_links import ChatEntity
from kebi.core.areas.models import AreaProfile
from kebi.core.places.places_repo import PlacesRepo
from kebi.db.repositories.area_repository import AreaRepository

logger = logging.getLogger(__name__)


class EntityIconRefresher:
    """Row-sourced icons for a turn's linked entities."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        area_repo: AreaRepository,
        places_repo_factory: Callable[[AsyncSession], PlacesRepo] = PlacesRepo,
    ) -> None:
        self._session_factory = session_factory
        self._area_repo = area_repo
        self._places_repo_factory = places_repo_factory

    async def refresh(self, entities: list[ChatEntity]) -> list[ChatEntity]:
        """Entities with icons re-read from their rows.

        Venue: the catalog row's icon when the row is found; a missing row
        keeps the payload snapshot (it was read from the same table moments
        ago). Area: the area row's icon, or `None` when the row doesn't
        exist yet — the screen will profile itself on first open and pick
        its own, so promising one now is exactly the mismatch this exists
        to remove.
        """
        venue_ids = [e.key for e in entities if e.kind == "venue"]
        area_keys = [e.key for e in entities if e.kind == "area"]
        if not venue_ids and not area_keys:
            return entities
        try:
            venue_icons, area_profiles = await asyncio.gather(
                self._venue_icons(venue_ids),
                self._area_repo.get_many(area_keys) if area_keys else _empty(),
            )
        except Exception:
            logger.warning("entity icon refresh failed", exc_info=True)
            return entities

        refreshed: list[ChatEntity] = []
        for entity in entities:
            if entity.kind == "venue" and entity.key in venue_icons:
                icon = venue_icons[entity.key]
            elif entity.kind == "area":
                profile = area_profiles.get(entity.key)
                icon = profile.icon if profile else None
            else:
                refreshed.append(entity)
                continue
            refreshed.append(
                entity
                if entity.icon == icon
                else entity.model_copy(update={"icon": icon})
            )
        return refreshed

    async def _venue_icons(self, venue_ids: list[str]) -> dict[str, str | None]:
        if not venue_ids:
            return {}
        async with self._session_factory() as session:
            rows = await self._places_repo_factory(session).get_by_ids(venue_ids)
        return {row.id: row.icon for row in rows if row.id}


async def _empty() -> dict[str, AreaProfile]:
    """Awaitable stand-in so the gather stays uniform when no areas linked."""
    return {}
