"""PlaceUpsertService — single gateway for all place writes.

Reads existing rows by provider_id, applies the merge policy, hands the
result to the repo. The repo never sees raw candidates and never applies
merge logic of its own.
"""

from __future__ import annotations

from ._place_merge import merge_place
from .models import PlaceCore
from .protocols import EmbeddingServiceProtocol, PlacesRepoProtocol


class PlaceUpsertService:
    def __init__(
        self,
        repo: PlacesRepoProtocol,
        embedding_service: EmbeddingServiceProtocol,
    ) -> None:
        self._repo = repo
        self._embedding_service = embedding_service

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

        persisted = await self._repo.upsert_places(merged)
        if persisted:
            await self._embedding_service.embed_and_store(persisted)
        return persisted
