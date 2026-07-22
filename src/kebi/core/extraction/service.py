"""Extraction service orchestrating the cascade pipeline (v2).

Spec 030 Phase 3 (ADR-070, ADR-071):
- `ExtractionPersistenceService` is gone. Persistence is inlined here:
  `PlaceUpsertService.upsert_and_embed` writes the place row and
  embedding in one call; `UserPlacesRepo.get_existing_place_ids`
  pre-filters duplicates; `UserPlacesService.save_places` creates
  the `user_places` link with `approved=False`.
- Confidence-band partitioning is dropped. Every picker output saves
  (ADR-071 supersedes ADR-057). `ExtractPlaceItem.status` no longer
  emits `"needs_review"` — only `"saved"` or `"duplicate"`.
- All place vocabulary is v2-native; no legacy `_to_legacy_source`
  shim.

ADR-073 dropped the agent's save tool — `ExtractionService.run` is now
called only by the `POST /v1/extract` route, which doesn't consume
reasoning-step emissions. The former `emit` parameter has been removed
along with all `save.*` step calls.

ADR-074 added a Redis result cache keyed by canonical URL: a hit lets
the second-and-later users sharing the same URL skip the entire
pipeline + upsert and just link the cached `PlaceCore`s to their own
`user_places`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from kebi.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
    FailureReason,
    NotedInterest,
)
from kebi.core.agent._trace_context import feature_trace
from kebi.core.config import get_config
from kebi.core.events.dispatcher import EventDispatcherProtocol
from kebi.core.events.events import ContentHarvestRequested, PlaceSaved
from kebi.core.extraction.candidate_mapper import candidate_to_core
from kebi.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    TooManyCandidatesError,
)
from kebi.core.extraction.input_parser import ParsedInput, parse_input
from kebi.core.extraction.result_cache import ExtractionResultCache
from kebi.core.extraction.types import ValidatedCandidate
from kebi.core.extraction.url_source import source_from_url
from kebi.core.knowledge.harvest_bucket import HarvestBucketWriter
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    HarvestSnapshot,
    ResolvedGeo,
)
from kebi.core.places import (
    DuplicateUserPlaceError,
    PlaceCore,
    PlaceNameAlias,
    PlaceSource,
    PlaceUpsertServiceProtocol,
    UserPlacesServiceProtocol,
)
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)

# Default per-request candidate cap. Hard cap on what the pipeline will
# hand to the search service (and therefore the most places a single
# extract request can save). Callers can tighten or loosen this by
# passing `limit=N` to `ExtractionService.run`.
DEFAULT_MAX_CANDIDATES = 25


def _unsupported_url_message(url: str) -> str:
    return (
        f"Sorry — I can't read links from this site yet. "
        f"I currently support TikTok, Instagram, YouTube, and Google Maps "
        f"share links. Try sharing the place name directly, or paste a "
        f"link from one of those. ({url})"
    )


def _failed_response(
    raw_input: str,
    request_id: str,
    reason: FailureReason,
    message: str,
) -> ExtractPlaceResponse:
    """Build a terminal `failed` envelope. Centralized so every failure
    site reads the same way."""
    return ExtractPlaceResponse(
        status="failed",
        results=[],
        raw_input=raw_input,
        request_id=request_id,
        failure_reason=reason,
        failure_message=message,
    )


def _noted_interests(names: list[str]) -> list[NotedInterest]:
    """Build the acknowledgment entries for detected non-venue geography.

    Acknowledge-only: nothing is persisted for a noted interest — the
    message keeps the user's action visible (never a silent drop) until
    the area model gives these a real home."""
    return [
        NotedInterest(
            name=name,
            message=(
                f"'{name}' looks like a route or region rather than a "
                f"single place — I've noted it as a travel interest "
                f"instead of saving it."
            ),
        )
        for name in names
    ]


def _candidate_to_item_dict(
    candidate: ValidatedCandidate,
    place: PlaceCore,
) -> dict[str, Any]:
    """Map a (candidate, persisted-place) pair to an ExtractPlaceItem dict.

    No `status` field — under ADR-071 the response is a flat list of
    places now associated with the user (whether newly linked or
    already saved is internal). `place` is a `PlaceCore`: identity +
    static fields only, no live signals (rating/hours/popularity are
    enriched later by the places read path, not by extraction).

    The per-candidate producer/medium audit trail is intentionally
    absent from the response and is not persisted.
    """
    return {
        "place": place.model_dump(mode="json"),
        "confidence": candidate.confidence,
    }


def _resolved_geo(place: PlaceCore) -> ResolvedGeo:
    """The geo a place's harvested claims anchor to, from its stored
    location. `country_code` (ISO alpha-2) is what canonical geo keys need;
    a place lacking it yields only place-scoped claims downstream."""
    loc = place.location
    if loc is None:
        return ResolvedGeo()
    return ResolvedGeo(
        country_code=loc.country_code,
        city=loc.city,
        neighborhood=loc.neighborhood,
    )


class ExtractionService:
    """Orchestrate the extraction cascade (ADR-008, ADR-070, ADR-071, ADR-074).

    Persistence is inlined here: `upsert_and_embed` writes the place
    row and embedding; `save_places` links to `user_places` with
    `approved=False` after pre-filtering duplicates. ADR-074 adds a
    Redis result cache so the second-and-later users sharing the same
    URL skip pipeline + upsert entirely.
    """

    def __init__(
        self,
        pipeline: ExtractionPipeline,
        upsert_service: PlaceUpsertServiceProtocol,
        user_places_service: UserPlacesServiceProtocol,
        event_dispatcher: EventDispatcherProtocol,
        result_cache: ExtractionResultCache,
        harvest_writer: HarvestBucketWriter,
    ) -> None:
        self._pipeline = pipeline
        self._upsert = upsert_service
        self._user_places = user_places_service
        self._event_dispatcher = event_dispatcher
        self._result_cache = result_cache
        self._harvest_writer = harvest_writer

    async def run(
        self,
        raw_input: str,
        user_id: str,
        request_id: str | None = None,
        limit: int | None = None,
        save_limit: int | None = None,
    ) -> ExtractPlaceResponse:
        """Run the extraction pipeline inline and return a terminal envelope.

        Returns `status ∈ {completed, failed}` — never `pending`.

        ADR-074: before running the pipeline, look up a Redis cache
        keyed by canonical URL (the same form `parse_input` produces).
        On hit, skip pipeline + upsert and just link the cached
        `PlaceCore`s to this user. On miss, run the pipeline and
        write the result to the cache before returning.

        `save_limit` is the caller's plan-tier save cap (forwarded by the
        gateway; `None` = unlimited). Enforced as an early guard before any
        pipeline or cache work, so a user with no remaining room never burns
        LLM/vision/transcription spend on a place they can't keep. The guard
        rejects when the library is already full; it is the enforcement point
        for this endpoint (the cache-hit path swallows save-time exceptions
        to fall back to the pipeline, which would make a deeper raise
        unreliable). The strict per-write chokepoint in `UserPlacesService`
        still governs the `POST /v1/user/places` single-save path.
        """
        if not raw_input or not raw_input.strip():
            raise ValueError("raw_input cannot be empty")

        parsed = parse_input(raw_input)
        source = source_from_url(parsed.url)
        rid = request_id or uuid4().hex

        if (
            save_limit is not None
            and await self._user_places.count_saves(user_id) >= save_limit
        ):
            return _failed_response(
                raw_input,
                rid,
                reason="save_limit_reached",
                message=(
                    "You've reached your saved-places limit. Upgrade to save "
                    "more, or remove a place first."
                ),
            )

        # Phase 4.5 subtask 2: open one Langfuse trace per extraction.
        # Every paid call inside (LLM resolver, picker, vision, Whisper,
        # save-time Voyage embed) nests under this parent via Langfuse's
        # OTel contextvar — no per-span plumbing needed. Trace opens
        # AFTER `parse_input` (so `parsed.url` is on the trace) but
        # BEFORE the unsupported-URL and cache-hit branches so both
        # outcomes are also visible (unsupported URLs land as a 0-span
        # trace, cache hits land with a single `extraction_cache_hit`
        # event marker).
        async with feature_trace(
            "extraction",
            user_id,
            name="extraction_run",
            extra={
                "source_ref": parsed.url,
                "source": source.value if source else "manual",
                "request_id": rid,
            },
        ):
            if parsed.url is not None and source is None:
                return _failed_response(
                    raw_input,
                    rid,
                    reason="unsupported_url",
                    message=_unsupported_url_message(parsed.url),
                )

            # ADR-074 cache lookup — short-circuits pipeline + upsert.
            # `source is not None` is guaranteed here: the unsupported-URL
            # branch above returns before this point, so any non-None
            # `parsed.url` has a known `PlaceSource`. Manual freetext
            # (`parsed.url is None`) skips the cache entirely —
            # freetext isn't content-addressable across users.
            if parsed.url is not None and source is not None:
                cached = await self._try_cache_hit(
                    source=source,
                    source_ref=parsed.url,
                    raw_input=raw_input,
                    user_id=user_id,
                    request_id=rid,
                )
                if cached is not None:
                    return cached

            response = await self._run_pipeline_and_persist(
                raw_input=raw_input,
                user_id=user_id,
                rid=rid,
                source=source,
                parsed=parsed,
                limit=limit,
            )

            # ADR-074 cache write — only completed responses with at
            # least one place; failed/empty outcomes aren't worth
            # re-serving.
            if (
                parsed.url is not None
                and source is not None
                and response.status == "completed"
                and response.results
            ):
                await self._result_cache.set(source, parsed.url, response.results)

            return response

    async def _try_cache_hit(
        self,
        source: PlaceSource,
        source_ref: str,
        raw_input: str,
        user_id: str,
        request_id: str,
    ) -> ExtractPlaceResponse | None:
        """Look up the result cache; on hit, link the cached cores to this
        user and return the response. On miss or unrecoverable error
        (FK violation against a deleted `places` row), evict and
        return None so the caller falls back to a full pipeline run."""
        cached_items = await self._result_cache.get(source, source_ref)
        if cached_items is None:
            return None
        try:
            return await self._save_from_cache(
                cached_items=cached_items,
                raw_input=raw_input,
                user_id=user_id,
                request_id=request_id,
                source=source,
                source_ref=source_ref,
            )
        except Exception:
            logger.warning(
                "extraction_cache_hit_invalid",
                extra={"source": source.value, "source_ref": source_ref},
                exc_info=True,
            )
            await self._result_cache.delete(source, source_ref)
            return None

    async def _run_pipeline_and_persist(
        self,
        raw_input: str,
        user_id: str,
        rid: str,
        source: PlaceSource | None,
        parsed: ParsedInput,
        limit: int | None,
    ) -> ExtractPlaceResponse:
        """Run the pipeline → upsert → save_places → build response.

        All terminal-failure branches (cap exceeded, pipeline error,
        empty candidates, all-duplicates) build their envelope via
        `_failed_response` so the failure shape stays consistent.
        """
        try:
            effective_limit = limit if limit is not None else DEFAULT_MAX_CANDIDATES
            pipeline_result = await self._pipeline.run(
                url=parsed.url,
                user_id=user_id,
                supplementary_text=parsed.supplementary_text,
                limit=effective_limit,
            )
            candidates = pipeline_result.candidates
        except TooManyCandidatesError as exc:
            logger.info(
                "Extraction request %s dropped: %d candidates exceeded limit of %d",
                rid,
                exc.found,
                exc.limit,
            )
            return _failed_response(
                raw_input,
                rid,
                reason="candidate_limit_exceeded",
                message=(
                    f"Found {exc.found} candidates, more than the limit "
                    f"of {exc.limit} — request dropped to protect "
                    f"validation quota."
                ),
            )
        except Exception:
            logger.exception("Extraction pipeline failed for request %s", rid)
            # Generic message — the exception type / detail stays in
            # the server log only. The `request_id` lets oncall correlate.
            return _failed_response(
                raw_input,
                rid,
                reason="pipeline_error",
                message="Pipeline error — see server logs.",
            )

        noted = _noted_interests(pipeline_result.noted_non_venues)

        if not candidates:
            # A share that contained only non-venue geography (a route, a
            # region) is a success with nothing saved — the acknowledgment
            # IS the outcome, not a failure.
            if noted:
                return ExtractPlaceResponse(
                    status="completed",
                    results=[],
                    raw_input=raw_input,
                    request_id=rid,
                    noted_interests=noted,
                )
            return _failed_response(
                raw_input,
                rid,
                reason="no_candidates",
                message=(
                    "No venue could be extracted from the post — the "
                    "caption, transcript, and visible text didn't "
                    "contain a recognizable place name."
                ),
            )

        items = await self._persist_and_build_items(
            candidates, user_id, rid, source, parsed.url, pipeline_result.content
        )
        if not items:
            if noted:
                return ExtractPlaceResponse(
                    status="completed",
                    results=[],
                    raw_input=raw_input,
                    request_id=rid,
                    noted_interests=noted,
                )
            return _failed_response(
                raw_input,
                rid,
                reason="no_candidates",
                message=(
                    "Picker emitted candidates but none survived "
                    "persistence (every provider_id was already "
                    "linked to the user)."
                ),
            )
        return ExtractPlaceResponse(
            status="completed",
            results=items,
            raw_input=raw_input,
            request_id=rid,
            noted_interests=noted,
        )

    async def _link_to_user(
        self,
        user_id: str,
        request_id: str,
        cores: list[PlaceCore],
        source: PlaceSource | None,
        source_ref: str | None,
        source_labels: Mapping[str, str | None] | None = None,
    ) -> set[str]:
        """Link `cores` to `user_places` and fire `PlaceSaved` for the
        newly-linked subset. Returns the set of `place_id`s that
        `save_places` rejected as already-linked-for-this-user (i.e.
        duplicates) — callers use this to omit them from the
        `PlaceSaved` event.

        Eligible cores are expected to have `.id` set; callers filter
        beforehand. Handles the `DuplicateUserPlaceError` retry
        pattern: `save_places` rolls back the whole batch on conflict,
        so we filter the conflicting ids and retry once with the rest.

        Shared by `_persist_and_build_items` (cache-miss / pipeline
        path) and `_save_from_cache` (ADR-074 cache-hit path).
        """
        if not cores:
            return set()

        save_source = source or PlaceSource.manual
        duplicate_place_ids: set[str] = set()
        try:
            await self._user_places.save_places(
                user_id=user_id,
                places=cores,
                source=save_source,
                source_ref=source_ref,
                source_labels=source_labels,
            )
        except DuplicateUserPlaceError as exc:
            duplicate_place_ids = set(exc.conflicts)
            non_duplicates = [
                c for c in cores if c.id and c.id not in duplicate_place_ids
            ]
            if non_duplicates:
                await self._user_places.save_places(
                    user_id=user_id,
                    places=non_duplicates,
                    source=save_source,
                    source_ref=source_ref,
                    source_labels=source_labels,
                )

        linked_place_core_ids = [
            c.id for c in cores if c.id and c.id not in duplicate_place_ids
        ]
        if linked_place_core_ids:
            await self._event_dispatcher.dispatch(
                PlaceSaved(
                    user_id=user_id,
                    place_core_ids=linked_place_core_ids,
                    place_metadata={},
                    request_id=request_id,
                )
            )
        return duplicate_place_ids

    async def _persist_and_build_items(
        self,
        candidates: list[ValidatedCandidate],
        user_id: str,
        request_id: str,
        source: PlaceSource | None,
        source_ref: str | None,
        content: HarvestContent,
    ) -> list[ExtractPlaceItem]:
        """Inline v2 persistence flow (ADR-070, ADR-071).

        Upserts every picker output, links them to `user_places` via
        `_link_to_user`, returns a flat list of `ExtractPlaceItem`s —
        one per persisted place, regardless of newly-linked vs
        already-linked. The saved/duplicate distinction is an internal
        optimization, not part of the response contract (ADR-071).
        """
        # ADR-081: the as-seen label feeds the shared `place_name_aliases`
        # only for high-confidence picks — a low-confidence (possibly
        # wrong) match must not poison global search. The per-user
        # `source_label` is ungated (it's the user's own memory) and is
        # threaded separately via `source_labels` below.
        confident_threshold = get_config().extraction.confidence.confident_threshold
        alias_source = (source or PlaceSource.manual).value
        cores = [
            candidate_to_core(
                c,
                aliases=(
                    [PlaceNameAlias(value=c.source_label, source=alias_source)]
                    if c.source_label and c.confidence >= confident_threshold
                    else None
                ),
            )
            for c in candidates
        ]
        persisted: list[PlaceCore] = await self._upsert.upsert_and_embed(cores)

        persisted_by_pid: dict[str, PlaceCore] = {
            p.provider_id: p for p in persisted if p.provider_id
        }
        eligible_cores: list[PlaceCore] = [
            persisted_by_pid[c.provider_id]
            for c in candidates
            if c.provider_id in persisted_by_pid
            and persisted_by_pid[c.provider_id].id is not None
        ]

        # Per-user display label (ungated): persisted place id → the
        # name this user saw the place as in the post.
        source_labels: dict[str, str | None] = {}
        for c in candidates:
            persisted_core = persisted_by_pid.get(c.provider_id)
            if persisted_core is not None and persisted_core.id is not None:
                source_labels[persisted_core.id] = c.source_label

        await self._link_to_user(
            user_id=user_id,
            request_id=request_id,
            cores=eligible_cores,
            source=source,
            source_ref=source_ref,
            source_labels=source_labels,
        )

        items: list[ExtractPlaceItem] = []
        for c in candidates:
            persisted_core = persisted_by_pid.get(c.provider_id)
            if persisted_core is None or persisted_core.id is None:
                logger.warning(
                    "candidate_not_persisted",
                    extra={"provider_id": c.provider_id},
                )
                continue
            items.append(ExtractPlaceItem(**_candidate_to_item_dict(c, persisted_core)))

        # ADR-121: snapshot the content + identified places to the bucket and
        # fire the background harvest. Only on this pipeline-run path (a
        # cache hit re-derives nothing and was harvested at first extraction),
        # and only when there is content worth mining and a place to anchor.
        await self._maybe_harvest(request_id, user_id, content, eligible_cores)
        return items

    async def _maybe_harvest(
        self,
        request_id: str,
        user_id: str,
        content: HarvestContent,
        places: list[PlaceCore],
    ) -> None:
        """Persist a harvest snapshot and dispatch `ContentHarvestRequested`.

        No-op when the content is empty or no place anchors it. The event
        carries only the bucket key — the durable content lives in the
        bucket, so the background handler reads it back off the critical
        path (the snapshot write and dispatch already run after the HTTP
        response via the event bus / non-fatal bucket write)."""
        if content.is_empty() or not places:
            return
        harvest_places = [
            HarvestPlace(
                place_id=p.id,
                name=p.place_name,
                geo=_resolved_geo(p),
            )
            for p in places
            if p.id
        ]
        if not harvest_places:
            return
        snapshot = HarvestSnapshot(content=content, places=harvest_places)
        key = await self._harvest_writer.write(request_id=request_id, snapshot=snapshot)
        if key is None:
            return  # bucket write failed (already logged) — nothing to harvest
        await self._event_dispatcher.dispatch(
            ContentHarvestRequested(
                user_id=user_id,
                harvest_key=key,
                source_ref=content.source_ref,
            )
        )

    async def _save_from_cache(
        self,
        cached_items: list[ExtractPlaceItem],
        raw_input: str,
        user_id: str,
        request_id: str,
        source: PlaceSource,
        source_ref: str,
    ) -> ExtractPlaceResponse:
        """ADR-074 cache-hit save path. Skips pipeline + upsert.

        The cached `PlaceCore`s already reference persisted `places`
        rows — re-linking is the only DB write. Raises whatever
        `save_places` raises on an unhandled error; the caller in
        `_try_cache_hit` catches and falls back to a full pipeline run
        after evicting the stale cache entry.
        """
        eligible_cores: list[PlaceCore] = [
            PlaceCore.model_validate(item.place.model_dump())
            for item in cached_items
            if item.place.id is not None
        ]
        # Phase 4.5 subtask 2: leaves a filterable marker on the
        # extraction trace. The trace itself has 0 LLM/embed spans on a
        # cache hit (no paid work runs); this event is how we tell a
        # cache hit apart from an empty / failed extraction in Langfuse.
        get_tracing_client().capture_message(
            "extraction_cache_hit",
            level="info",
            metadata={
                "source": source.value,
                "source_ref": source_ref,
                "place_count": len(cached_items),
            },
            user_id=user_id,
            session_id=user_id,
        )
        await self._link_to_user(
            user_id=user_id,
            request_id=request_id,
            cores=eligible_cores,
            source=source,
            source_ref=source_ref,
        )
        return ExtractPlaceResponse(
            status="completed",
            results=cached_items,
            raw_input=raw_input,
            request_id=request_id,
        )
