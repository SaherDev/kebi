"""Extraction service orchestrating the cascade pipeline."""

import logging
from typing import Any
from uuid import uuid4

from totoro_ai.api.schemas.extract_place import (
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
from totoro_ai.core.emit import EmitFn
from totoro_ai.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    TooManyCandidatesError,
)
from totoro_ai.core.extraction.input_parser import parse_input
from totoro_ai.core.extraction.persistence import (
    ExtractionPersistenceService,
    PlaceSaveOutcome,
)
from totoro_ai.core.extraction.status_repository import ExtractionStatusRepository
from totoro_ai.core.extraction.url_source import normalize_url, source_from_url
from totoro_ai.core.places import PlaceSource

logger = logging.getLogger(__name__)

# Default per-request candidate cap. Hard cap on what the pipeline will
# hand to Google Places validation (and therefore the most places a
# single extract request can save). Callers can tighten or loosen this
# by passing `limit=N` to `ExtractionService.run`.
DEFAULT_MAX_CANDIDATES = 25


_SOURCE_LABELS: dict[PlaceSource, str] = {
    PlaceSource.tiktok: "the TikTok video",
    PlaceSource.instagram: "the Instagram post",
    PlaceSource.youtube: "the YouTube video",
    PlaceSource.google_maps: "the Google Maps list",
    PlaceSource.manual: "what you added or wrote",
}


def _source_label(source: PlaceSource) -> str:
    # Unsupported-URL requests are rejected before we'd ever need to
    # label `None` — see `_unsupported_url_message`.
    return _SOURCE_LABELS.get(source, "what you shared")


def _unsupported_url_message(url: str) -> str:
    return (
        f"Sorry — I can't read links from this site yet. "
        f"I currently support TikTok, Instagram, YouTube, and Google Maps "
        f"share links. Try sharing the place name directly, or paste a "
        f"link from one of those. ({url})"
    )


def _build_parse_summary(source: PlaceSource | None, has_text: bool) -> str:
    # The text branch covers both free-form notes and bare lists of places
    # (e.g. "Fuji Ramen, Pizza Place"). "What you shared" stays neutral
    # across both since we don't classify the text at parse time.
    if source is not None and has_text:
        return f"Reading {_source_label(source)} and what you shared"
    if source is not None:
        return f"Reading {_source_label(source)}"
    return "Reading what you shared"


def _is_real(outcome: PlaceSaveOutcome) -> bool:
    """Filter below-threshold outcomes — they never appear in `results` (FR-005).

    Below-threshold outcomes contribute only to the envelope-level `failed`
    determination (ADR-063).
    """
    return (
        outcome.status in ("saved", "needs_review", "duplicate")
        and outcome.place is not None
    )


def _outcome_to_item_dict(outcome: PlaceSaveOutcome) -> dict[str, Any]:
    """Map a real outcome to an ExtractPlaceItem dict. Caller must `_is_real` first."""
    assert outcome.place is not None  # enforced by _is_real
    return {
        "place": outcome.place.model_dump(mode="json"),
        "confidence": outcome.metadata.confidence,
        "status": outcome.status,
        "evidence": [
            {
                "producer": e.producer.value,
                "medium": e.medium.value,
                "snippet": e.snippet,
                "metadata": dict(e.metadata),
            }
            for e in outcome.metadata.evidence
        ],
    }


class ExtractionService:
    """Orchestrate place extraction cascade pipeline (ADR-008, ADR-034, ADR-063).

    M1 (feature 027): `run()` awaits the pipeline inline and returns a
    terminal envelope (`completed` or `failed`) synchronously. The Save
    tool (M5) consumes this inline outcome. HTTP callers that want the
    fire-and-return `pending` behavior wrap `run()` in `asyncio.create_task`
    at the route layer (see `ChatService._dispatch` extract-place branch).
    """

    def __init__(
        self,
        pipeline: ExtractionPipeline,
        persistence: ExtractionPersistenceService,
        status_repo: ExtractionStatusRepository,
    ) -> None:
        self._pipeline = pipeline
        self._persistence = persistence
        self._status_repo = status_repo

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

        The `raw_input` is echoed verbatim on the envelope (ADR-063). The
        optional `request_id` argument lets the caller inject an id
        generated at the route layer so both the envelope and the Redis
        write share one id; when omitted, one is generated here.

        When `emit` is supplied, this service emits `save.parse_input`
        after input parsing and `save.persist` after persistence; the
        pipeline emits `save.enrich` / `save.deep_enrichment` /
        `save.validate` at its own phase boundaries.

        `limit`, when supplied, overrides `DEFAULT_MAX_CANDIDATES` for
        this single request — callers (e.g. the agent's save tool)
        can tighten or loosen the cap explicitly. `None` falls back to
        the default.
        """
        _emit: EmitFn = emit or (lambda step, summary, duration_ms=None: None)

        if not raw_input or not raw_input.strip():
            raise ValueError("raw_input cannot be empty")

        parsed = parse_input(raw_input)
        # Canonicalize platform-specific URL forms (e.g. TikTok /photo/ →
        # /video/) so downstream yt-dlp calls don't hit "Unsupported URL".
        parsed.url = normalize_url(parsed.url)
        source = source_from_url(parsed.url)
        rid = request_id or uuid4().hex

        # Unsupported URL: caller passed a real URL but its host isn't a
        # source we can extract from. Reject up-front with a clear message
        # rather than running the cascade and timing out on it.
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
            result = await self._pipeline.run(
                url=parsed.url,
                user_id=user_id,
                supplementary_text=parsed.supplementary_text,
                emit=emit,
                limit=effective_limit,
            )
            if not result:
                response = ExtractPlaceResponse(
                    status="failed",
                    results=[],
                    raw_input=raw_input,
                    request_id=rid,
                    failure_reason="no_candidates",
                    failure_message=(
                        "No venue could be extracted from the post — "
                        "the caption, transcript, and visible text "
                        "didn't contain a recognizable place name."
                    ),
                )
            else:
                outcomes = await self._persistence.save_and_emit(
                    result, user_id, source_url=parsed.url, source=source
                )
                items = [
                    ExtractPlaceItem(**_outcome_to_item_dict(o))
                    for o in outcomes
                    if _is_real(o)
                ]
                if items:
                    response = ExtractPlaceResponse(
                        status="completed",
                        results=items,
                        raw_input=raw_input,
                        request_id=rid,
                    )
                else:
                    # Outcomes existed but every one was filtered: all
                    # below confidence threshold (Google rejected as
                    # locality / weak match / no match).
                    attempted_names = [
                        o.metadata.place_name for o in outcomes
                    ]
                    response = ExtractPlaceResponse(
                        status="failed",
                        results=[],
                        raw_input=raw_input,
                        request_id=rid,
                        failure_reason="all_below_threshold",
                        failure_message=(
                            f"Found {len(attempted_names)} candidate(s) "
                            f"({', '.join(attempted_names[:5])}) but "
                            f"confidence was below the save threshold — "
                            f"likely cities/regions or weak Google Places "
                            f"matches, not specific venues."
                        ),
                    )
        except TooManyCandidatesError as exc:
            # Expected outcome — not an error. Pipeline already emitted
            # the user-facing reason via `save.cap_exceeded`; flag the
            # final persist summary so it doesn't look like a normal failure.
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
