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
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from kebi.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
from kebi.core.emit import EmitFn
from kebi.core.events.dispatcher import EventDispatcherProtocol
from kebi.core.events.events import PlaceSaved
from kebi.core.extraction.candidate_mapper import candidate_to_core
from kebi.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    TooManyCandidatesError,
)
from kebi.core.extraction.input_parser import parse_input
from kebi.core.extraction.status_repository import ExtractionStatusRepository
from kebi.core.extraction.types import ValidatedCandidate
from kebi.core.extraction.url_source import normalize_url, source_from_url
from kebi.core.places_v2 import (
    DuplicateUserPlaceError,
    PlaceCore,
    PlaceObject,
    PlaceSource,
    PlaceUpsertServiceProtocol,
    UserPlacesServiceProtocol,
)

logger = logging.getLogger(__name__)

# Default per-request candidate cap. Hard cap on what the pipeline will
# hand to the search service (and therefore the most places a single
# extract request can save). Callers can tighten or loosen this by
# passing `limit=N` to `ExtractionService.run`.
DEFAULT_MAX_CANDIDATES = 25


_SOURCE_LABELS: dict[PlaceSource, str] = {
    PlaceSource.tiktok: "the TikTok video",
    PlaceSource.instagram: "the Instagram post",
    PlaceSource.youtube: "the YouTube video",
    PlaceSource.google_maps_list: "the Google Maps list",
    PlaceSource.manual: "what you added or wrote",
}


def _source_label(source: PlaceSource) -> str:
    return _SOURCE_LABELS.get(source, "what you shared")


def _unsupported_url_message(url: str) -> str:
    return (
        f"Sorry — I can't read links from this site yet. "
        f"I currently support TikTok, Instagram, YouTube, and Google Maps "
        f"share links. Try sharing the place name directly, or paste a "
        f"link from one of those. ({url})"
    )


def _build_parse_summary(source: PlaceSource | None, has_text: bool) -> str:
    if source is not None and has_text:
        return f"Reading {_source_label(source)} and what you shared"
    if source is not None:
        return f"Reading {_source_label(source)}"
    return "Reading what you shared"


def _candidate_to_item_dict(
    candidate: ValidatedCandidate,
    place: PlaceObject,
) -> dict[str, Any]:
    """Map a (candidate, persisted-place) pair to an ExtractPlaceItem dict.

    No `status` field — under ADR-071 the response is a flat list of
    places now associated with the user (whether newly linked or
    already saved is internal).
    """
    return {
        "place": place.model_dump(mode="json"),
        "confidence": candidate.confidence,
        "evidence": [
            {
                "producer": e.producer.value,
                "medium": e.medium.value,
                "snippet": e.snippet,
                "metadata": dict(e.metadata),
            }
            for e in candidate.evidence
        ],
    }


class ExtractionService:
    """Orchestrate the extraction cascade (ADR-008, ADR-070, ADR-071).

    Persistence is inlined here: `upsert_and_embed` writes the place
    row and embedding; `save_places` links to `user_places` with
    `approved=False` after pre-filtering duplicates. No separate
    persistence service.
    """

    def __init__(
        self,
        pipeline: ExtractionPipeline,
        upsert_service: PlaceUpsertServiceProtocol,
        user_places_service: UserPlacesServiceProtocol,
        status_repo: ExtractionStatusRepository,
        event_dispatcher: EventDispatcherProtocol,
    ) -> None:
        self._pipeline = pipeline
        self._upsert = upsert_service
        self._user_places = user_places_service
        self._status_repo = status_repo
        self._event_dispatcher = event_dispatcher

    async def run(
        self,
        raw_input: str,
        user_id: str,
        request_id: str | None = None,
        emit: EmitFn | None = None,
        limit: int | None = None,
    ) -> ExtractPlaceResponse:
        """Run the extraction pipeline inline and return a terminal envelope.

        Returns `status ∈ {completed, failed}` — never `pending`. Writes
        the final envelope to the Redis status store under
        `extraction:v2:{request_id}`.
        """
        _emit: EmitFn = emit or (lambda step, summary, duration_ms=None: None)

        if not raw_input or not raw_input.strip():
            raise ValueError("raw_input cannot be empty")

        parsed = parse_input(raw_input)
        parsed.url = normalize_url(parsed.url)
        source = source_from_url(parsed.url)
        rid = request_id or uuid4().hex

        if parsed.url is not None and source is None:
            unsupported_msg = _unsupported_url_message(parsed.url)
            _emit("save.unsupported_url", unsupported_msg)
            response = ExtractPlaceResponse(
                status="failed",
                results=[],
                raw_input=raw_input,
                request_id=rid,
                failure_reason="unsupported_url",
                failure_message=unsupported_msg,
            )
            _emit(
                "save.persist",
                "Skipped — this kind of link isn't supported yet",
            )
            await self._status_repo.write(rid, response.model_dump(mode="json"))
            return response

        parse_summary = _build_parse_summary(source, bool(parsed.supplementary_text))
        _emit("save.parse_input", parse_summary)

        cap_exceeded = False
        try:
            effective_limit = (
                limit if limit is not None else DEFAULT_MAX_CANDIDATES
            )
            candidates = await self._pipeline.run(
                url=parsed.url,
                user_id=user_id,
                supplementary_text=parsed.supplementary_text,
                emit=emit,
                limit=effective_limit,
            )
            if not candidates:
                response = ExtractPlaceResponse(
                    status="failed",
                    results=[],
                    raw_input=raw_input,
                    request_id=rid,
                    failure_reason="no_candidates",
                    failure_message=(
                        "No venue could be extracted from the post — the "
                        "caption, transcript, and visible text didn't "
                        "contain a recognizable place name."
                    ),
                )
            else:
                items = await self._persist_and_build_items(
                    candidates, user_id, rid, source, parsed.url
                )
                if items:
                    response = ExtractPlaceResponse(
                        status="completed",
                        results=items,
                        raw_input=raw_input,
                        request_id=rid,
                    )
                else:
                    response = ExtractPlaceResponse(
                        status="failed",
                        results=[],
                        raw_input=raw_input,
                        request_id=rid,
                        failure_reason="no_candidates",
                        failure_message=(
                            "Picker emitted candidates but none survived "
                            "persistence (every provider_id was already "
                            "linked to the user)."
                        ),
                    )
        except TooManyCandidatesError as exc:
            cap_exceeded = True
            logger.info(
                "Extraction request %s dropped: %d candidates exceeded "
                "limit of %d",
                rid,
                exc.found,
                exc.limit,
            )
            response = ExtractPlaceResponse(
                status="failed",
                results=[],
                raw_input=raw_input,
                request_id=rid,
                failure_reason="candidate_limit_exceeded",
                failure_message=(
                    f"Found {exc.found} candidates, more than the limit "
                    f"of {exc.limit} — request dropped to protect "
                    f"validation quota."
                ),
            )
        except Exception as exc:
            logger.exception("Extraction pipeline failed for request %s", rid)
            response = ExtractPlaceResponse(
                status="failed",
                results=[],
                raw_input=raw_input,
                request_id=rid,
                failure_reason="pipeline_error",
                failure_message=f"Pipeline error: {type(exc).__name__}: {exc}",
            )

        if response.results:
            persist_summary = f"Saved {len(response.results)} place(s)"
        elif response.status == "completed":
            persist_summary = "Done — nothing new to save"
        elif cap_exceeded:
            persist_summary = "Skipped — too many places in one request"
        else:
            persist_summary = "Could not save — no valid places found"
        _emit("save.persist", persist_summary)
        await self._status_repo.write(rid, response.model_dump(mode="json"))
        return response

    async def _persist_and_build_items(
        self,
        candidates: list[ValidatedCandidate],
        user_id: str,
        request_id: str,
        source: PlaceSource | None,
        source_url: str | None,
    ) -> list[ExtractPlaceItem]:
        """Inline v2 persistence flow (ADR-070, ADR-071).

        1. Upsert + embed every picker output (one v2 call).
        2. Link to user_places via UserPlacesService.save_places. The
           service raises DuplicateUserPlaceError and rolls back the
           whole batch on any conflict — catch it, filter out the
           conflicting place_ids, retry once with the rest.
        3. Dispatch PlaceSaved per *newly* linked place (skipping
           conflicts so taste-model regen doesn't re-fire for places
           the user already saved).
        4. Return ExtractPlaceItems: a flat list of places now
           associated with the user. No per-item status field — the
           saved/duplicate distinction is an internal optimization,
           not part of the response contract (ADR-071).

        v2 services are the single seam: extraction never reaches the
        UserPlacesRepo directly.
        """
        cores = [candidate_to_core(c) for c in candidates]
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

        duplicate_place_ids: set[str] = set()
        if eligible_cores:
            save_source = source or PlaceSource.manual
            try:
                await self._user_places.save_places(
                    user_id=user_id,
                    places=eligible_cores,
                    source=save_source,
                    source_url=source_url,
                )
            except DuplicateUserPlaceError as exc:
                duplicate_place_ids = set(exc.conflicts)
                non_duplicates = [
                    c for c in eligible_cores
                    if c.id and c.id not in duplicate_place_ids
                ]
                if non_duplicates:
                    await self._user_places.save_places(
                        user_id=user_id,
                        places=non_duplicates,
                        source=save_source,
                        source_url=source_url,
                    )

        items: list[ExtractPlaceItem] = []
        linked_place_ids: list[str] = []
        for c in candidates:
            persisted_core = persisted_by_pid.get(c.provider_id)
            if persisted_core is None or persisted_core.id is None:
                logger.warning(
                    "candidate_not_persisted",
                    extra={"provider_id": c.provider_id},
                )
                continue

            items.append(
                ExtractPlaceItem(
                    **_candidate_to_item_dict(c, _core_to_object(persisted_core))
                )
            )
            if persisted_core.id not in duplicate_place_ids:
                linked_place_ids.append(persisted_core.id)

        if linked_place_ids:
            event = PlaceSaved(
                user_id=user_id,
                place_ids=linked_place_ids,
                place_metadata={},
                request_id=request_id,
            )
            await self._event_dispatcher.dispatch(event)

        return items


def _core_to_object(core: PlaceCore) -> PlaceObject:
    """Lift a PlaceCore into a PlaceObject for the response envelope.

    The upsert path returns PlaceCore (no live fields populated). The
    response schema expects PlaceObject. Live fields stay None until a
    subsequent read goes through PlacesSearchService and fills them
    from the cache.
    """
    return PlaceObject.model_validate(core.model_dump())
