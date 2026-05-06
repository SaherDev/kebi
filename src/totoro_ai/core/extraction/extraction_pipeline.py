"""ExtractionPipeline — level-by-level runner for the extraction cascade."""

from __future__ import annotations

from totoro_ai.core.config import ExtractionConfig
from totoro_ai.core.emit import EmitFn
from totoro_ai.core.extraction.dedup import (
    dedup_candidates,
    dedup_validated_by_provider_id,
)
from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
from totoro_ai.core.extraction.protocols import Enricher
from totoro_ai.core.extraction.types import (
    ExtractionContext,
    ValidatedCandidate,
)
from totoro_ai.core.extraction.validator import PlacesValidatorProtocol

_ENRICHER_LABELS = {
    "SubtitleCheckEnricher": "subtitles",
    "WhisperAudioEnricher": "audio transcript",
    "VisionFramesEnricher": "video frames",
    "VisionImagesEnricher": "post images",
    "PhotoDetectorEnricher": "photo detection",
    "TikTokOEmbedEnricher": "TikTok metadata",
    "YtDlpMetadataEnricher": "video metadata",
    "GoogleMapsListEnricher": "Google Maps list",
    "LLMNEREnricher": "text analysis",
    "CircuitBreakerEnricher": "fallback extractor",
    "ParallelEnricherGroup": "parallel extractors",
}


def _friendly(class_name: str) -> str:
    return _ENRICHER_LABELS.get(class_name, class_name)


def inline_summary(context: ExtractionContext, _fired: list[str]) -> str:
    """Summary for the inline (text + metadata) level — count-driven."""
    if context.candidates:
        return f"Found {len(context.candidates)} possible place(s) in the text"
    return "No places found in the text"


def deep_summary(_context: ExtractionContext, fired: list[str]) -> str:
    """Summary for deep enrichment (subtitle/audio/vision) — enricher-driven."""
    if not fired:
        return "No extra checks needed"
    return "Taking a closer look: " + ", ".join(_friendly(n) for n in fired)


class TooManyCandidatesError(Exception):
    """Raised when an enrichment level produces more candidates than allowed.

    The pipeline refuses to validate or persist anything in this case —
    the whole request is dropped. The service catches this exception and
    returns a `failed` envelope with a user-facing reason in the SSE stream.
    """

    def __init__(self, found: int, limit: int) -> None:
        super().__init__(f"Found {found} candidates; limit is {limit}")
        self.found = found
        self.limit = limit


def _enforce_candidate_limit(
    context: ExtractionContext, limit: int, emit: EmitFn
) -> None:
    """Drop the request when too many candidates were produced.

    Runs after every enrichment level, before validation. Emits a
    `save.cap_exceeded` reasoning step with counts and raises
    `TooManyCandidatesError` so the service can return a `failed`
    envelope without spending Google Places quota or DB writes.
    """
    found = len(context.candidates)
    if found <= limit:
        return
    emit(
        "save.cap_exceeded",
        f"Found {found} possible places, more than the limit of {limit} — "
        "skipping this request to protect the system",
    )
    raise TooManyCandidatesError(found=found, limit=limit)


class ExtractionPipeline:
    """Level-driven extraction runner (ADR-008 — sequential async, not LangGraph).

    A list of `EnrichmentLevel`s is run in order. After each executed
    level the pipeline runs the shared `finalizer` (typically
    `LLMNEREnricher` — one consolidated NER call over the just-populated
    text fields), emits the level's summary, enforces the candidate cap,
    and asks the validator for a verdict. The first level whose validated
    set is non-empty short-circuits and returns. Levels that declare
    `requires_url=True` are skipped silently when the input has no URL.

    Default configuration wires two levels:
    - "enrich"           — TikTok oEmbed + yt-dlp (text/metadata producers).
    - "deep_enrichment"  — subtitle, whisper, vision (URL-only fallback).
    Plus one shared finalizer: LLMNEREnricher, which harvests candidates
    from the text fields after each level runs.
    """

    def __init__(
        self,
        levels: list[EnrichmentLevel],
        validator: PlacesValidatorProtocol,
        extraction_config: ExtractionConfig,
        finalizer: Enricher | None = None,
    ) -> None:
        self._levels = levels
        self._validator = validator
        self._extraction_config = extraction_config
        self._finalizer = finalizer

    async def run(
        self,
        url: str | None,
        user_id: str,
        limit: int,
        supplementary_text: str = "",
        emit: EmitFn | None = None,
    ) -> list[ValidatedCandidate]:
        """Run the extraction cascade.

        `limit` is the per-request candidate cap. The pipeline takes a
        concrete value; default-fallback logic lives in
        `ExtractionService.run` (the only caller).
        """
        _emit: EmitFn = emit or (lambda step, summary, duration_ms=None: None)

        context = ExtractionContext(
            url=url,
            user_id=user_id,
            supplementary_text=supplementary_text,
        )

        for level in self._levels:
            executed, summary = await level.run(context)
            if not executed:
                continue
            if self._finalizer is not None:
                await self._finalizer.enrich(context)
                dedup_candidates(context)
            _emit(f"save.{level.name}", summary)
            _enforce_candidate_limit(context, limit, _emit)

            results = await self._validator.validate(context.candidates)
            if results:
                _emit(
                    "save.validate",
                    f"Confirmed {len(results)} place(s) via Places providor ",
                )
                return dedup_validated_by_provider_id(
                    results, self._extraction_config.confidence
                )

        _emit("save.validate", "Could not confirm any places")
        return []
