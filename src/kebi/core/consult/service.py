"""Consult service — place recommendation pipeline (feature 019, ADR-058, feature 028 M4).

4-phase pipeline: **geocode → discover → merge+dedupe → enrich+persist**. Tier-specific
branches (warming blend, active chip filter) are conditional modifiers on the
enrichment output.

Every place flowing between pipeline stages is a `PlaceObject`. The pipeline
dedupes by `provider_id` (preferred) or `place_id`, enriches the combined
candidate set once via `PlacesService.enrich_batch(geo_only=False)`, and
returns candidates in source order (saved first, discovered second).

Feature 028 M4 changes:
- IntentParser removed — caller supplies pre-parsed `query` + `ConsultFilters`.
- UserMemoryService removed — caller composes `preference_context` if desired.
- Main-path taste-profile load removed. The chip-filter branch (active tier)
  retains its taste-service read (ADR-061).
- Internal RecallService call removed — caller pre-loads `saved_places`.
- `ConsultResponse.reasoning_steps` field removed. Steps delivered live via
  the `emit` callback at each pipeline boundary.

RankingService deleted per ADR-058 — agent-driven ranking is deferred.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from kebi.api.schemas.consult import (
    ConsultResponse,
    ConsultResult,
    Location,
)
from kebi.core.consult.types import (
    NoMatchesError,
    map_google_place_to_place_object,
)
from kebi.core.emit import EmitFn
from kebi.core.places import PlacesClient, PlacesService
from kebi.core.places.filters import ConsultFilters
from kebi.core.places.models import PlaceObject
from kebi.core.taste.schemas import Chip, ChipStatus
from kebi.core.taste.service import TasteModelService
from kebi.core.utils.geo import haversine_m
from kebi.db.repositories.recommendation_repository import (
    NullRecommendationRepository,
    RecommendationRepository,
)

logger = logging.getLogger(__name__)


class ConsultService:
    """4-phase place recommendation pipeline (PlaceObject-first, ADR-058).

    Main path takes pre-parsed arguments from the caller: `query` is the
    retrieval phrase, `saved_places` is the pre-loaded user recall set,
    `filters` carries structural + discovery bounds, `preference_context`
    is an optional one-sentence summary composed by the agent. Active-tier
    chip-filter branch (ADR-061) retains the only taste-service read on
    the main path.
    """

    def __init__(
        self,
        places_client: PlacesClient,
        places_service: PlacesService,
        taste_service: TasteModelService,
        recommendation_repo: RecommendationRepository | None = None,
    ) -> None:
        self._places_client = places_client
        self._places_service = places_service
        self._taste_service = taste_service
        self._recommendation_repo: RecommendationRepository = (
            recommendation_repo
            if recommendation_repo is not None
            else NullRecommendationRepository()
        )

    async def consult(
        self,
        user_id: str,
        query: str,
        saved_places: list[PlaceObject],
        filters: ConsultFilters,
        limit: int,
        location: Location | None = None,
        preference_context: str | None = None,
        signal_tier: str = "active",
        emit: EmitFn | None = None,
    ) -> ConsultResponse:
        _emit: EmitFn = emit or (lambda _s, _m, _d=None: None)
        del preference_context  # reserved for future ranking use; not consumed yet

        from kebi.core.config import get_config

        config = get_config()
        logger.info("Signal tier for user %s: %s", user_id, signal_tier)

        # Phase 1: geocode (if a named search location is supplied).
        search_location: dict[str, float] | None = None
        if filters.search_location_name:
            location_bias = (
                {"lat": location.lat, "lng": location.lng} if location else None
            )
            search_location = await self._places_client.geocode(
                filters.search_location_name, location_bias=location_bias
            )
            _emit(
                "consult.geocode",
                f"Pinpointed {filters.search_location_name!r} on the map"
                if search_location
                else f"Could not locate {filters.search_location_name!r}",
            )
        if search_location is None and location:
            search_location = {"lat": location.lat, "lng": location.lng}

        radius_m = filters.radius_m or (
            config.consult.named_location_radius_m
            if filters.search_location_name
            else config.consult.default_radius_m
        )

        # Pre-filter saved places by distance if we have a search_location.
        filtered_saved: list[PlaceObject] = []
        for place in saved_places:
            if search_location and place.lat is not None and place.lng is not None:
                distance_m = haversine_m(
                    search_location["lat"],
                    search_location["lng"],
                    place.lat,
                    place.lng,
                )
                if distance_m > radius_m:
                    continue
            filtered_saved.append(place)

        # Phase 2: discover external candidates via places provider.
        keyword_places: list[PlaceObject] = []
        suggestion_places: list[PlaceObject] = []
        if search_location:
            suggestions = (filters.place_suggestions or [])[:5]

            async def _keyword_discover() -> list[PlaceObject]:
                try:
                    results = await self._places_client.discover(
                        search_location,
                        {"keyword": query, "radius": radius_m},
                    )
                    return [map_google_place_to_place_object(r) for r in results]
                except RuntimeError:
                    return []

            keyword_places, suggestion_places = await asyncio.gather(
                _keyword_discover(),
                self._places_client.validate_places(suggestions, location_bias=search_location),
            )
            keyword_places = list(keyword_places)
            suggestion_places = list(suggestion_places)
            _emit(
                "consult.keywords",
                f"Found {len(keyword_places)} nearby matches"
                if keyword_places
                else "No nearby matches found",
            )
            _emit(
                "consult.suggestions",
                f"Validated {len(suggestion_places)} suggested places"
                if suggestion_places
                else "No suggested places to validate",
            )
        else:
            _emit("consult.keywords", "Skipped discovery — no location to search around")
            _emit(
                "consult.suggestions",
                "Skipped suggestion validation — no location to search around",
            )

        # Phase 3: merge + dedupe (saved → suggested → discovered priority).
        deduped_places, sources_by_place_id = _dedupe_places(
            filtered_saved, keyword_places, suggestion_places
        )
        _emit(
            "consult.merge",
            f"Combined {len(filtered_saved)} from your saves, "
            f"{len(keyword_places)} from search, "
            f"{len(suggestion_places)} from suggestions"
            if filtered_saved or keyword_places or suggestion_places
            else "Nothing to combine",
        )
        _emit(
            "consult.dedupe",
            f"{len(deduped_places)} unique places after removing duplicates"
            if deduped_places
            else "No places left after removing duplicates",
        )

        # Phase 4: enrich with Tier 2 + Tier 3 data.
        saved_priority_pids = {
            p.provider_id for p in filtered_saved if p.provider_id is not None
        }
        enriched_places = (
            await self._places_service.enrich_batch(
                deduped_places,
                geo_only=False,
                priority_provider_ids=saved_priority_pids,
            )
            if deduped_places
            else []
        )
        _emit(
            "consult.enrich",
            f"Added details for {len(enriched_places)} places"
            if enriched_places
            else "No places to add details for",
        )

        # Active-tier chip filter (ADR-061) — ONE remaining taste read on main path.
        if signal_tier == "active":
            taste_profile = await self._taste_service.get_taste_profile(user_id)
            if taste_profile is not None:
                confirmed_chips = [
                    c for c in taste_profile.chips if c.status == ChipStatus.CONFIRMED
                ]
                rejected_chips = [
                    c for c in taste_profile.chips if c.status == ChipStatus.REJECTED
                ]
                if rejected_chips:
                    before = len(enriched_places)
                    enriched_places = [
                        p
                        for p in enriched_places
                        if not any(
                            _place_matches_chip(p, chip) for chip in rejected_chips
                        )
                    ]
                    removed = before - len(enriched_places)
                    if removed > 0:
                        _emit(
                            "consult.chip_filter",
                            f"Removed {removed} place(s) matching preferences "
                            "you rejected",
                        )
                if confirmed_chips:
                    _emit(
                        "consult.chip_filter",
                        "Honoring your confirmed preferences: "
                        + ", ".join(c.label for c in confirmed_chips),
                    )

        if not enriched_places:
            raise NoMatchesError(query)

        if signal_tier == "warming":
            saved_cap = round(limit * config.taste_model.warming_blend.saved)
            discovered_cap = limit - saved_cap
            saved_pool = [
                p
                for p in enriched_places
                if sources_by_place_id.get(p.place_id) == "saved"
            ][:saved_cap]
            discovered_pool = [
                p
                for p in enriched_places
                if sources_by_place_id.get(p.place_id) in ("discovered", "suggested")
            ][:discovered_cap]
            top = (saved_pool + discovered_pool)[:limit]
            _emit(
                "consult.tier_blend",
                f"Mixing {len(saved_pool)} from your saves with "
                f"{len(discovered_pool)} new discoveries",
            )
        else:
            top = enriched_places[:limit]

        results = [
            ConsultResult(
                place=place,
                source=sources_by_place_id.get(place.place_id, "discovered"),
            )
            for place in top
        ]

        recommendation_id = await self._persist_recommendation(user_id, query, results)

        return ConsultResponse(
            recommendation_id=recommendation_id,
            results=results,
        )

    async def _persist_recommendation(
        self,
        user_id: str,
        query: str,
        results: list[ConsultResult],
    ) -> str | None:
        try:
            from kebi.db.models import Recommendation

            # Pre-generate UUID so we never access rec.id after commit()
            # (async SQLAlchemy expiry makes post-commit PK reads unreliable).
            rec_id = uuid4()

            # Persist only Tier 1 place fields — Tier 2 (geo) and Tier 3
            # (enrichment) live in Redis and are re-fetched on demand, so
            # storing them here would duplicate mutable cache state.
            tier1_results = [
                ConsultResult(
                    place=result.place.copy_with(geo_fresh=False, enriched=False),
                    source=result.source,
                )
                for result in results
            ]

            response_data = ConsultResponse(
                recommendation_id=None,
                results=tier1_results,
            ).model_dump(mode="json")

            rec = Recommendation(
                id=rec_id,
                user_id=user_id,
                query=query,
                response=response_data,
            )
            await self._recommendation_repo.save(rec)
            return str(rec_id)
        except Exception as exc:
            logger.warning(
                "Failed to persist recommendation for user %s: %s", user_id, exc
            )
            return None


def _place_matches_chip(place: PlaceObject, chip: Chip) -> bool:
    """Return True if the chip's (source_field, source_value) matches this place.

    Walks the chip's dotted `source_field` path against the place's
    attribute tree. Used in active-tier rejected-chip filtering (feature 023).
    """
    parts = chip.source_field.split(".")
    target = chip.source_value

    if parts == ["source"]:
        if place.source is None:
            return False
        return place.source.value == target

    if len(parts) == 2 and parts[0] == "subcategory":
        expected_place_type = parts[1]
        return (
            place.place_type.value == expected_place_type
            and place.subcategory == target
        )

    if parts[0] == "attributes":
        attrs: object = place.attributes
        for segment in parts[1:]:
            if attrs is None:
                return False
            attrs = getattr(attrs, segment, None)
        if attrs is None:
            return False
        return attrs == target

    return False


def _dedupe_places(
    saved: list[PlaceObject],
    discovered: list[PlaceObject],
    suggested: list[PlaceObject],
) -> tuple[list[PlaceObject], dict[str, str]]:
    """Dedupe places by provider_id, then place_id.

    Priority order: saved → suggested → discovered. When a place appears in
    both suggested and discovered, it is tagged "suggested" (named suggestions
    are more deliberate than keyword search hits).

    Returns the ordered deduplicated list and a `place_id → "saved"|"suggested"|"discovered"`
    map so downstream callers can apply source-specific treatment per place.
    """
    seen_provider_ids: set[str] = set()
    seen_place_ids: set[str] = set()
    deduped: list[PlaceObject] = []
    sources: dict[str, str] = {}

    def _push(place: PlaceObject, source: str) -> None:
        if place.provider_id is not None:
            if place.provider_id in seen_provider_ids:
                return
            seen_provider_ids.add(place.provider_id)
            seen_place_ids.add(place.place_id)
            deduped.append(place)
            sources[place.place_id] = source
            return
        if place.place_id in seen_place_ids:
            return
        seen_place_ids.add(place.place_id)
        deduped.append(place)
        sources[place.place_id] = source

    for place in saved:
        _push(place, "saved")
    for place in suggested:
        _push(place, "suggested")
    for place in discovered:
        _push(place, "discovered")

    return deduped, sources
