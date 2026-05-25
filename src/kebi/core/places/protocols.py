"""Protocol interfaces for the places library (ADR-038)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from .models import (
    HybridSearchFilters,
    HybridSearchHit,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)

# Default TTL (seconds) for cached PlaceObjects.
# 30 days — Google ToS compliance: cached Places data must not be retained
# beyond this window.
PLACE_CACHE_TTL_SECONDS: int = 2_592_000


class PlacesRepoProtocol(Protocol):
    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]: ...

    async def get_by_provider_ids(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceCore]: ...

    async def find(
        self, query: PlaceQuery, limit: int = 20
    ) -> list[PlaceCore]: ...

    async def upsert_places(self, cores: list[PlaceCore]) -> list[PlaceCore]: ...

    async def wipe_stale_locations(self, cutoff: datetime) -> list[PlaceCore]: ...


class UserPlacesRepoProtocol(Protocol):
    async def get_by_user(self, user_id: str) -> list[UserPlace]: ...

    async def get_by_user_place_id(self, user_place_id: str) -> UserPlace | None: ...

    async def get_existing_place_ids(
        self, user_id: str, place_ids: list[str]
    ) -> set[str]: ...

    async def save_user_places(
        self, user_places: list[UserPlace]
    ) -> list[UserPlace]: ...


class PlacesCacheProtocol(Protocol):
    async def mget(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceObject]: ...

    async def mset(
        self, places: list[PlaceObject], ttl_seconds: int = PLACE_CACHE_TTL_SECONDS
    ) -> None: ...

    async def delete_many(self, provider_ids: list[str]) -> None: ...


class PlacesClientProtocol(Protocol):
    async def search(
        self, query: PlaceQuery, limit: int = 20
    ) -> list[PlaceObject]: ...

    async def get_by_ids(
        self, provider_ids: list[str]
    ) -> list[PlaceObject]: ...


class PlacesSearchServiceProtocol(Protocol):
    async def find(
        self, query: PlaceQuery, limit: int = 20
    ) -> list[PlaceObject]: ...

    async def get_by_ids(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceObject]: ...

    async def get_cores_by_ids(
        self, place_core_ids: list[str]
    ) -> dict[str, PlaceCore]: ...


class PlaceUpsertServiceProtocol(Protocol):
    async def upsert_and_embed(
        self, candidates: list[PlaceCore]
    ) -> list[PlaceCore]: ...


class PlaceWipeServiceProtocol(Protocol):
    async def wipe_stale_locations(self, retention_days: int = 30) -> int: ...


class UserPlacesServiceProtocol(Protocol):
    async def save_places(
        self,
        user_id: str,
        places: list[PlaceCore],
        source: PlaceSource,
        source_ref: str | None,
        source_labels: Mapping[str, str | None] | None = None,
    ) -> list[UserPlace]: ...

    async def get_user_places(self, user_id: str) -> list[SavedPlaceView]: ...

    async def update_status(
        self,
        user_place_id: str,
        *,
        visited: bool | None = None,
        liked: bool | None = None,
        approved: bool | None = None,
        note: str | None = None,
    ) -> UserPlace: ...


class EmbeddingsRepoProtocol(Protocol):
    async def get_by_place_ids(
        self, place_ids: list[str]
    ) -> dict[str, list[float]]: ...

    async def get_signatures_by_place_ids(
        self, place_ids: list[str]
    ) -> dict[str, tuple[str, str]]: ...

    async def upsert_embeddings(
        self, records: list[tuple[str, list[float], str, str]]
    ) -> None: ...

    async def delete_by_place_ids(self, place_ids: list[str]) -> int: ...


class EmbedderProtocol(Protocol):
    """External embedder. Mirrors the project-wide embedder shape so any
    `providers.embeddings` implementation drops in unchanged.
    """

    async def embed(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]: ...


class EmbeddingServiceProtocol(Protocol):
    async def embed_and_store(self, cores: list[PlaceCore]) -> None: ...


class HybridSearchRepoProtocol(Protocol):
    async def search(
        self,
        user_id: str | None,
        query: str,
        query_vector: list[float],
        filters: HybridSearchFilters | None = None,
        limit: int = 20,
        rrf_k: int = 60,
        candidate_multiplier: int = 4,
    ) -> list[HybridSearchHit]: ...


class HybridSearchServiceProtocol(Protocol):
    async def search(
        self,
        user_id: str | None,
        query: str,
        filters: HybridSearchFilters | None = None,
        limit: int = 20,
        rrf_k: int = 60,
        candidate_multiplier: int = 4,
    ) -> list[HybridSearchHit]: ...
