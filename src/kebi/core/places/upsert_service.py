"""PlaceUpsertService — single gateway for all place writes.

Reads existing rows by provider_id, applies the merge policy, resolves each
row's area identity through the geo registry, and hands the result to the
repo. The repo never sees raw candidates and never applies merge or
resolution logic of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ._place_merge import merge_place
from .models import PlaceCore
from .protocols import EmbeddingServiceProtocol, PlacesRepoProtocol

if TYPE_CHECKING:
    from kebi.core.geo.protocols import GeoRegistryProtocol

logger = logging.getLogger(__name__)


class PlaceUpsertService:
    def __init__(
        self,
        repo: PlacesRepoProtocol,
        embedding_service: EmbeddingServiceProtocol,
        geo_registry: GeoRegistryProtocol,
    ) -> None:
        self._repo = repo
        self._embedding_service = embedding_service
        self._geo_registry = geo_registry

    async def upsert_and_embed(self, candidates: list[PlaceCore]) -> list[PlaceCore]:
        """Read existing → merge per candidate → resolve area → write → embed.

        Requires every candidate to carry a provider_id (identity must be
        resolved upstream before reaching this layer). The repo enforces
        this and will raise on violation.

        Area resolution is the write path's mint point: a save naming an
        area the registry hasn't met is worth one geocoder call, once,
        ever. A location whose city can't be verified stores no key — the
        `elsewhere` bucket, coarser-but-correct — and heals on a later
        refresh once the registry knows the name.

        EmbeddingService skips rows whose (text_hash, model_name) already
        matches the DB, so callers can pass the full batch without
        pre-filtering changed rows.
        """
        if not candidates:
            return []

        provider_ids = [c.provider_id for c in candidates if c.provider_id]
        existing_map = (
            await self._repo.get_by_provider_ids(provider_ids) if provider_ids else {}
        )

        merged = [
            merge_place(existing_map.get(c.provider_id or ""), c) for c in candidates
        ]
        keyed = [await self._with_geo_key(core) for core in merged]

        persisted = await self._repo.upsert_places(keyed)
        if persisted:
            await self._embedding_service.embed_and_store(persisted)
        return persisted

    async def _with_geo_key(self, core: PlaceCore) -> PlaceCore:
        loc = core.location
        if loc is None or not loc.country_code or not loc.city:
            return core.model_copy(update={"geo_key": None})
        try:
            resolved = await self._geo_registry.key_for_location(
                loc.country_code,
                loc.city,
                loc.neighborhood,
                lat=loc.lat,
                lng=loc.lng,
                mint=True,
            )
        except Exception:
            # Identity resolution must never cost the save. Keep whatever
            # key the row already carries (a refresh must not strip a good
            # key over a transient failure); a new row stays keyless until
            # a later write heals it.
            logger.warning(
                "place_geo_resolution_failed",
                extra={"provider_id": core.provider_id},
                exc_info=True,
            )
            return core
        # A key only at city depth or deeper — a country-level key is not an
        # area anyone navigates to (the `elsewhere` rule, ADR-165).
        key = resolved.geo_key if resolved and resolved.city else None
        return core.model_copy(update={"geo_key": key})
