"""ExtractionPersistenceService — shared write-path for extraction results.

Feature 019 rewired this service to go through `PlacesService.create_batch`
instead of the deprecated `SQLAlchemyPlaceRepository`. `PlaceSaveOutcome.place`
is now a `PlaceObject` — the unified shared shape — not the removed
`ExtractionResult`. `DuplicatePlaceError` from `PlacesService` is caught and
mapped per conflict into `PlaceSaveOutcome(status="duplicate")`.

ADR-057 follow-up: after the Tier 1 write succeeds, persistence writes the
Tier 2 geo cache from the `match_lat` / `match_lng` / `match_address` data
Google Places returned during validation. Without this step, every saved
row would carry `geo_fresh=False` even though the lat/lng/address was
already in hand at save time.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from totoro_ai.core.config import get_config
from totoro_ai.core.events.events import PlaceSaved
from totoro_ai.core.extraction.types import ValidatedCandidate
from totoro_ai.core.places import (
    DuplicatePlaceError,
    GeoData,
    PlaceCreate,
    PlaceObject,
    PlaceSource,
    PlacesService,
)
from totoro_ai.core.places.cache import PlacesCache
from totoro_ai.core.places.repository import build_provider_id
from totoro_ai.db.repositories import EmbeddingRepository
from totoro_ai.providers.embeddings import EmbedderProtocol

if TYPE_CHECKING:
    from totoro_ai.core.events.dispatcher import EventDispatcherProtocol

logger = logging.getLogger(__name__)


@dataclass
class PlaceSaveOutcome:
    """Per-candidate outcome from save_and_emit.

    `place`  → the persisted `PlaceObject` on "saved" / "needs_review" or the
               existing `PlaceObject` (Tier 1 only) on "duplicate"; `None`
               for "below_threshold".
    `status` → "saved" | "needs_review" | "duplicate" | "below_threshold".
               Per ADR-057:
                 confidence < save_threshold        → "below_threshold"
                 save_threshold ≤ c < confident     → "needs_review"
                 confidence ≥ confident_threshold   → "saved"
               "duplicate" is orthogonal and set by the repository layer
               when a provider_id already exists.
    `metadata` → the `ValidatedCandidate` this outcome was built from; held
                 so the handler/status-payload builder can read confidence,
                 resolved_by, and city without re-running the validator.
    """

    metadata: ValidatedCandidate
    place: PlaceObject | None
    place_id: str | None
    status: str


class ExtractionPersistenceService:
    """Shared write-path for extraction results.

    Ordering invariant: DB writes → PlaceSaved dispatch → bulk embeddings.
    """

    def __init__(
        self,
        places_service: PlacesService,
        places_cache: PlacesCache,
        embedding_repo: EmbeddingRepository,
        embedder: EmbedderProtocol,
        event_dispatcher: "EventDispatcherProtocol",
    ) -> None:
        self._places_service = places_service
        self._places_cache = places_cache
        self._embedding_repo = embedding_repo
        self._embedder = embedder
        self._event_dispatcher = event_dispatcher

    async def save_and_emit(
        self,
        results: list[ValidatedCandidate],
        user_id: str,
        source_url: str | None = None,
        source: PlaceSource | None = None,
    ) -> list[PlaceSaveOutcome]:
        confidence_cfg = get_config().extraction.confidence
        save_threshold = confidence_cfg.save_threshold
        confident_threshold = confidence_cfg.confident_threshold

        # 1. Partition into three bands per ADR-057:
        #      c < save_threshold          → below (never written)
        #      save_threshold ≤ c < confident → eligible, flagged "needs_review"
        #      c ≥ confident_threshold      → eligible, flagged "saved"
        #    Both eligible bands go through the same write path; the status
        #    comes from which band the candidate belongs to. `tentative_pids`
        #    holds the provider_ids that land in the needs_review band — so
        #    _create_and_classify can stamp the right status after the write.
        below: list[ValidatedCandidate] = []
        eligible: list[ValidatedCandidate] = []
        tentative_pids: set[str] = set()
        for vc in results:
            rounded = round(vc.confidence, 2)
            if rounded < save_threshold:
                below.append(vc)
                continue
            eligible.append(vc)
            if rounded < confident_threshold:
                pid = self._format_provider_id(vc)
                if pid is not None:
                    tentative_pids.add(pid)

        eligible_outcomes = await self._create_and_classify(
            eligible, user_id, tentative_pids, source_url, source
        )

        # 2. Re-assemble outcomes in original input order.
        outcome_map: dict[int, PlaceSaveOutcome] = {}
        for idx, vc in enumerate(results):
            if vc in below:
                outcome_map[idx] = PlaceSaveOutcome(
                    metadata=vc,
                    place=None,
                    place_id=None,
                    status="below_threshold",
                )
        for vc, outcome in zip(eligible, eligible_outcomes, strict=True):
            idx = results.index(vc)
            outcome_map[idx] = outcome
        outcomes = [outcome_map[i] for i in range(len(results))]

        # Both "saved" and "needs_review" rows are Tier 1 writes, emit
        # PlaceSaved events, and get embedded so recall can surface them.
        saved_outcomes = [
            o for o in outcomes if o.status in ("saved", "needs_review") and o.place
        ]
        if not saved_outcomes:
            return outcomes

        # 3. Write Tier 2 geo cache from the data Google handed us during
        #    validation. Runs before PlaceSaved dispatch so a downstream
        #    recall fired by an event handler sees the populated cache.
        #    Failures are swallowed inside PlacesCache.set_geo_batch.
        await self._write_geo_cache(saved_outcomes)

        # 4. Dispatch PlaceSaved AFTER all DB writes (ordering invariant).
        event = PlaceSaved(
            user_id=user_id,
            place_ids=[o.place.place_id for o in saved_outcomes if o.place],
            place_metadata={},
        )
        await self._event_dispatcher.dispatch(event)

        # 4. Embed all saved places in one batch (non-fatal on failure).
        try:
            descriptions = [self._build_description(o) for o in saved_outcomes]
            vectors = await self._embedder.embed(descriptions, input_type="document")
            model_name = get_config().models["embedder"].model
            records = [
                (o.place.place_id, vec, model_name)  # type: ignore[union-attr]
                for o, vec in zip(saved_outcomes, vectors, strict=True)
            ]
            await self._embedding_repo.bulk_upsert_embeddings(records)
        except Exception as exc:
            logger.warning(
                "Failed to generate/store embeddings (non-fatal): %s. "
                "Places saved, taste signal captured.",
                exc,
                exc_info=True,
            )

        return outcomes

    async def _create_and_classify(
        self,
        eligible: list[ValidatedCandidate],
        user_id: str,
        tentative_pids: set[str],
        source_url: str | None,
        source: PlaceSource | None,
    ) -> list[PlaceSaveOutcome]:
        """Call `places_service.create_batch` and map each row back to an outcome.

        `tentative_pids` is the set of provider_ids whose confidence falls
        into the needs_review band (ADR-057); those rows get status
        "needs_review" instead of "saved" after a successful write.

        On `DuplicatePlaceError`, the whole batch is rolled back (per FR-006a).
        We look up the existing place for each conflict and mark it as a
        duplicate; rows that are NOT in the conflict list are retried
        individually via `create()` so successful rows are not lost.
        """
        if not eligible:
            return []

        items = [
            self._to_place_create(vc, user_id, source_url, source) for vc in eligible
        ]

        try:
            saved = await self._places_service.create_batch(items)
        except DuplicatePlaceError as exc:
            conflict_ids: dict[str, str] = {
                c.provider_id: c.existing_place_id for c in exc.conflicts
            }
            return await self._retry_one_by_one(
                eligible, user_id, conflict_ids, tentative_pids, source_url, source
            )

        return [
            PlaceSaveOutcome(
                metadata=vc,
                place=place,
                place_id=place.place_id,
                status=self._status_for(vc, tentative_pids),
            )
            for vc, place in zip(eligible, saved, strict=True)
        ]

    async def _retry_one_by_one(
        self,
        eligible: list[ValidatedCandidate],
        user_id: str,
        conflict_ids: dict[str, str],
        tentative_pids: set[str],
        source_url: str | None,
        source: PlaceSource | None,
    ) -> list[PlaceSaveOutcome]:
        outcomes: list[PlaceSaveOutcome] = []
        for vc in eligible:
            provider_id = self._format_provider_id(vc)
            if provider_id and provider_id in conflict_ids:
                existing_id = conflict_ids[provider_id]
                existing = await self._places_service.get(existing_id)
                outcomes.append(
                    PlaceSaveOutcome(
                        metadata=vc,
                        place=existing,
                        place_id=existing_id,
                        status="duplicate",
                    )
                )
                continue

            item = self._to_place_create(vc, user_id, source_url, source)
            try:
                place = await self._places_service.create(item)
            except DuplicatePlaceError as inner:
                conflict = inner.conflicts[0]
                existing = await self._places_service.get(conflict.existing_place_id)
                outcomes.append(
                    PlaceSaveOutcome(
                        metadata=vc,
                        place=existing,
                        place_id=conflict.existing_place_id,
                        status="duplicate",
                    )
                )
            else:
                outcomes.append(
                    PlaceSaveOutcome(
                        metadata=vc,
                        place=place,
                        place_id=place.place_id,
                        status=self._status_for(vc, tentative_pids),
                    )
                )
        return outcomes

    def _status_for(self, vc: ValidatedCandidate, tentative_pids: set[str]) -> str:
        """Return "needs_review" if the candidate is in the tentative band."""
        pid = self._format_provider_id(vc)
        if pid is not None and pid in tentative_pids:
            return "needs_review"
        return "saved"

    @staticmethod
    def _to_place_create(
        vc: ValidatedCandidate,
        user_id: str,
        source_url: str | None,
        source: PlaceSource | None,
    ) -> PlaceCreate:
        """Assemble a `PlaceCreate` from a `ValidatedCandidate` plus the
        pipeline context the candidate doesn't carry (`user_id`,
        `source_url`, `source`). This is the single boundary where
        extraction's lighter shape becomes the persistence shape."""
        return PlaceCreate(
            user_id=user_id,
            place_name=vc.place_name,
            place_type=vc.place_type,
            subcategory=vc.subcategory,
            tags=vc.tags,
            attributes=vc.attributes,
            source_url=source_url,
            source=source,
            provider=vc.provider,
            external_id=vc.external_id,
        )

    @staticmethod
    def _format_provider_id(vc: ValidatedCandidate) -> str | None:
        return build_provider_id(vc.provider, vc.external_id)

    async def _write_geo_cache(self, saved_outcomes: list[PlaceSaveOutcome]) -> None:
        """Write Tier 2 geo cache for newly saved rows whose validator
        carried full lat/lng/address. Duplicates are skipped — the cache
        already has an entry (or the next enrichment call will fill it).
        Errors are swallowed inside `PlacesCache.set_geo_batch` per FR-026b.
        """
        geo_items: dict[str, GeoData] = {}
        now = datetime.now(UTC)
        for outcome in saved_outcomes:
            if outcome.status not in ("saved", "needs_review"):
                continue
            place = outcome.place
            if place is None or place.provider_id is None:
                continue
            vc = outcome.metadata
            if vc.match_lat is None or vc.match_lng is None or vc.match_address is None:
                continue
            geo_items[place.provider_id] = GeoData(
                lat=vc.match_lat,
                lng=vc.match_lng,
                address=vc.match_address,
                cached_at=now,
            )
        if geo_items:
            await self._places_cache.set_geo_batch(geo_items)

    def _build_description(self, outcome: PlaceSaveOutcome) -> str:
        """Build the embedding input from a saved `PlaceObject`.

        Config-driven: `embeddings.description_fields` in `config/app.yaml`
        lists the Tier 1 fields to include (and their order), and
        `embeddings.description_separator` is the join string. Retrieval
        evals can re-tune by editing the config and re-embedding — no code
        change. Tier 2 / Tier 3 data (address, hours, rating, …) is never
        part of the description; those live in Redis and drift per call.
        """
        place = outcome.place
        assert place is not None  # callers filter this

        extractors: dict[str, Callable[[PlaceObject], str | None]] = {
            "place_name": lambda p: p.place_name,
            "subcategory": lambda p: p.subcategory,
            "place_type": lambda p: p.place_type.value.replace("_", " "),
            "cuisine": lambda p: p.attributes.cuisine,
            "ambiance": lambda p: p.attributes.ambiance,
            "price_hint": lambda p: p.attributes.price_hint,
            "tags": lambda p: " ".join(p.tags) if p.tags else None,
            "good_for": (
                lambda p: (
                    " ".join(p.attributes.good_for) if p.attributes.good_for else None
                )
            ),
            "dietary": (
                lambda p: (
                    " ".join(p.attributes.dietary) if p.attributes.dietary else None
                )
            ),
            "neighborhood": (
                lambda p: (
                    p.attributes.location_context.neighborhood
                    if p.attributes.location_context
                    else None
                )
            ),
            "city": (
                lambda p: (
                    p.attributes.location_context.city
                    if p.attributes.location_context
                    else None
                )
            ),
            "country": (
                lambda p: (
                    p.attributes.location_context.country
                    if p.attributes.location_context
                    else None
                )
            ),
        }

        cfg = get_config().embeddings
        parts: list[str] = []
        for field in cfg.description_fields:
            extractor = extractors.get(field)
            if extractor is None:
                continue
            value = extractor(place)
            if value:
                parts.append(value)
        return cfg.description_separator.join(parts)
