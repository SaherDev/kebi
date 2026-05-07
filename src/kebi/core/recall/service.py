"""Recall service — orchestrate embedding + hybrid/filter search + geo enrichment.

Flow (Session 2 Addendum → T042 rewritten §9):

1. `results, total_count = await repo.search(...)`
2. Extract `PlaceObject`s from the internal `RecallResult` dataclasses.
3. `enriched_places = await places_service.enrich_batch(places, geo_only=True)`
4. If `filters.max_distance_km` is set and `location` is non-None, filter the
   enriched places in Python via haversine — drop anything with
   `geo_fresh is False` or distance above the threshold. Re-assemble the
   result list via a `place_id`-keyed dict (NOT by index; gaps break
   positional alignment).
5. Return a `RecallResponse` wrapping the final results and the
   (best-effort, post-distance) `total_count`.

The repository never touches Redis, `lat`, `lng`, or the `location` parameter.
All geo work lives here. The `total_count` is accurate for the DB-side
filter set; after distance filtering it reflects the number delivered in
the current window rather than the full unfiltered set — the
recall-service docstring and the PR description call this out explicitly.
"""

from __future__ import annotations

import logging
import math
from typing import Literal

from kebi.api.schemas.recall import RecallResponse, RecallResult
from kebi.core.config import RecallConfig
from kebi.core.emit import EmitFn
from kebi.core.places import PlacesService
from kebi.core.places.models import PlaceObject
from kebi.core.recall.types import RecallFilters
from kebi.db.repositories.recall_repository import RecallRepository
from kebi.providers.embeddings import EmbedderProtocol

logger = logging.getLogger(__name__)


class RecallService:
    """Recall service: filter or hybrid search + Tier-2 geo enrichment.

    Note: `total_count` is best-effort after distance filtering. It reflects
    the DB-level match count from the filter clauses; when `max_distance_km`
    is applied in Python, we do not re-query for the exact post-distance
    count because that would require scanning every page of the result
    set. Callers use it for "showing 20 of ~147" UI hints, not for exact
    pagination math.
    """

    def __init__(
        self,
        embedder: EmbedderProtocol,
        recall_repo: RecallRepository,
        config: RecallConfig,
        places_service: PlacesService,
    ) -> None:
        self._embedder = embedder
        self._repo = recall_repo
        self._config = config
        self._places_service = places_service

    async def run(
        self,
        query: str | None,
        user_id: str,
        filters: RecallFilters | None = None,
        sort_by: Literal["relevance", "created_at"] = "relevance",
        location: tuple[float, float] | None = None,
        limit: int | None = None,
        emit: EmitFn | None = None,
    ) -> RecallResponse:
        _emit: EmitFn = emit or (lambda _s, _m, _d=None: None)

        # Cold-start: zero saved places → empty state before any work.
        saved_count = await self._repo.count_saved_places(user_id)
        if saved_count == 0:
            return RecallResponse(results=[], total_count=0, empty_state=True)

        filters = filters or RecallFilters()

        # Normalize empty/whitespace query to None — filter-mode dispatch
        # downstream is keyed on `query is None` (db repository), so an
        # empty string would otherwise fall into hybrid mode and hit the
        # RRF floor with nothing to contribute (ADR-057 follow-up).
        if query is not None and not query.strip():
            query = None

        mode = "filter" if query is None else "hybrid"
        effective_limit = limit if limit is not None else self._config.max_results
        verb = "Browsing" if mode == "filter" else f"Searching for {query!r} in"
        order = "newest first" if sort_by == "created_at" else "by relevance"
        _emit(
            "recall.mode",
            f"{verb} your saves {order}, up to {effective_limit}",
        )

        query_vector: list[float] | None = None
        if query is not None:
            try:
                vectors = await self._embedder.embed([query], input_type="query")
                query_vector = vectors[0]
            except RuntimeError as exc:
                logger.warning(
                    "Embedding failed in recall; falling back to text-only search",
                    extra={"user_id": user_id, "error": str(exc)},
                )

        raw_results, total_count = await self._repo.search(
            user_id=user_id,
            query=query,
            query_vector=query_vector,
            filters=filters,
            sort_by=sort_by,
            limit=effective_limit,
            rrf_k=self._config.rrf_k,
            candidate_multiplier=self._config.candidate_multiplier,
            min_rrf_score=self._config.min_rrf_score,
            max_cosine_distance=self._config.max_cosine_distance,
            location=location,
        )

        _emit(
            "recall.result",
            f"{len(raw_results)} places matched" if raw_results else "no places matched",  # noqa: E501
        )

        places = [r.place for r in raw_results]
        enriched_places = (
            await self._places_service.enrich_batch(places, geo_only=True)
            if places
            else []
        )

        # Re-key match metadata by place_id so we can survive distance gaps.
        metadata_by_id: dict[
            str, tuple[str, float | None, Literal["rrf", "ts_rank"] | None]
        ] = {
            r.place.place_id: (r.match_reason, r.relevance_score, r.score_type)
            for r in raw_results
        }

        if (
            filters.max_distance_km is not None
            and location is not None
            and enriched_places
        ):
            threshold_km = filters.max_distance_km
            final_places: list[PlaceObject] = []
            for place in enriched_places:
                if not place.geo_fresh or place.lat is None or place.lng is None:
                    continue
                distance_km = _haversine_km(
                    location[0], location[1], place.lat, place.lng
                )
                if distance_km <= threshold_km:
                    final_places.append(place)
        else:
            final_places = list(enriched_places)

        response_results = [
            RecallResult(
                place=place,
                match_reason=metadata_by_id.get(place.place_id, ("filter", None, None))[
                    0
                ],
                relevance_score=metadata_by_id.get(
                    place.place_id, ("filter", None, None)
                )[1],
                score_type=metadata_by_id.get(place.place_id, ("filter", None, None))[
                    2
                ],
            )
            for place in final_places
        ]

        return RecallResponse(
            results=response_results,
            total_count=total_count,
            empty_state=False,
        )


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c
