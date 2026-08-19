"""PlaceUpsertService — single gateway for all place writes.

Reads existing rows by provider_id, applies the merge policy, resolves the
area the place sits in, hands the result to the repo. The repo never sees
raw candidates and never applies merge logic of its own.

Area resolution belongs here rather than in the mapper because it is a
network call and the mapper is pure, and here rather than in the repo
because a repository issues queries and nothing else. This is the one place
every place write passes through, which is exactly what a rule about the
stored area needs.
"""

from __future__ import annotations

import asyncio
import logging

from ._place_merge import merge_place
from .area_geocoder import AreaGeocoderProtocol
from .models import PlaceCore
from .protocols import EmbeddingServiceProtocol, PlacesRepoProtocol

logger = logging.getLogger(__name__)

# Bounds provider QPS when a batch of candidates lands at once. Most batches
# share an area and therefore share a cache entry, so this rarely binds.
_GEOCODE_CONCURRENCY = 5


class PlaceUpsertService:
    def __init__(
        self,
        repo: PlacesRepoProtocol,
        embedding_service: EmbeddingServiceProtocol,
        area_geocoder: AreaGeocoderProtocol | None = None,
    ) -> None:
        self._repo = repo
        self._embedding_service = embedding_service
        self._area_geocoder = area_geocoder

    async def upsert_and_embed(self, candidates: list[PlaceCore]) -> list[PlaceCore]:
        """Read existing → merge per candidate → bulk write → embed.

        Requires every candidate to carry a provider_id (identity must be
        resolved upstream before reaching this layer). The repo enforces
        this and will raise on violation.

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

        merged = await self._resolve_areas(merged)

        persisted = await self._repo.upsert_places(merged)
        if persisted:
            await self._embedding_service.embed_and_store(persisted)
        return persisted

    async def _resolve_areas(self, cores: list[PlaceCore]) -> list[PlaceCore]:
        """Fill in geography the place is missing, from where it actually is.

        Deliberately fills gaps and never overwrites. The geocoder is the
        better source for *whether* a place has an area — a venue whose
        address record carries no locality still sits inside one, and that
        gap is what leaves a save stranded off every area screen. It is a
        worse source for what to *call* that area: it answers with the
        administrative village, so letting it overwrite would rename Canggu
        to Tibubeneng and Seminyak to Kerobokan Kelod. Consistent identity
        and human names are two different problems, and this solves only
        the first.

        Degrades silently and per place: an unresolved coordinate keeps the
        geography it arrived with, which is the previous behaviour, never a
        dropped write.
        """
        geocoder = self._area_geocoder
        if geocoder is None:
            return cores

        sem = asyncio.Semaphore(_GEOCODE_CONCURRENCY)

        async def _resolved(core: PlaceCore) -> PlaceCore:
            loc = core.location
            if loc is None or loc.lat is None or loc.lng is None:
                return core
            async with sem:
                area = await geocoder.resolve(loc.lat, loc.lng)
            if area is None:
                return core
            return core.model_copy(
                update={
                    "location": loc.model_copy(
                        update={
                            "country_code": loc.country_code or area.country_code,
                            "city": loc.city or area.city,
                            "neighborhood": loc.neighborhood or area.neighborhood,
                        }
                    )
                }
            )

        return list(await asyncio.gather(*[_resolved(c) for c in cores]))
