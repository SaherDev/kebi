"""ExtractionPipeline — level-by-level runner for the search-first cascade.

Search-first flow (one pass per `EnrichmentLevel`):

1. **Producers** populate text fields (caption / transcript / title /
   hashtags / location_tag) and `KnownPlace`s (Google Maps shared
   list, vision frames, vision images).
2. **Search** (`PlacesSearcher`) fans out Google Places Text Search
   over `known_places` + `location_tag`; appends vetted matches to
   `context.search_matches`. Idempotent across levels.
3. **Pick** (`LLMPlacePicker`) lets the LLM choose which Search hits
   the post actually references and infer rich attributes
   (atmosphere, time-of-day, signature items, …). Returns
   `ValidatedCandidate`s with confidence already computed via
   `reconcile_picks`.
4. **Cap** (`_enforce_candidate_limit`) drops the request when picks
   exceed the configured ceiling.
5. **Dedup** (`dedup_validated_by_provider_id`) collapses two picks
   that share a `provider_id`.

The first level whose pick set is non-empty short-circuits and
returns; subsequent levels (e.g. deep enrichment with subtitle /
whisper / vision) only run when earlier levels produced nothing.
"""

from __future__ import annotations

from typing import Protocol

from totoro_ai.core.config import ExtractionConfig
from totoro_ai.core.emit import EmitFn
from totoro_ai.core.extraction.dedup import dedup_validated_by_provider_id
from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
from totoro_ai.core.extraction.types import (
    ExtractionContext,
    ValidatedCandidate,
)


class SearcherProtocol(Protocol):
    """Search-step contract — runs Google Places fan-out, populates
    `context.search_matches`."""

    async def search(self, context: ExtractionContext) -> None: ...


class PickerProtocol(Protocol):
    """Pick-step contract — LLM picks from `context.search_matches`,
    returns list of fully-formed `ValidatedCandidate`s."""

    async def pick(
        self, context: ExtractionContext
    ) -> list[ValidatedCandidate]: ...

_ENRICHER_LABELS = {
    "SubtitleCheckEnricher": "subtitles",
    "WhisperAudioEnricher": "audio transcript",
    "VisionFramesEnricher": "video frames",
    "VisionImagesEnricher": "post images",
    "TikTokPhotoEnricher": "TikTok carousel photos",
    "InstagramPostEnricher": "Instagram post",
    "TikTokCaptionEnricher": "TikTok caption",
    "VideoMetadataEnricher": "video metadata",
    "GoogleMapsListEnricher": "Google Maps list",
    "LLMPlacePicker": "place picker",
    "PlacesSearcher": "Google Places search",
    "CircuitBreakerEnricher": "fallback extractor",
    "ParallelEnricherGroup": "parallel extractors",
}


def _friendly(class_name: str) -> str:
    return _ENRICHER_LABELS.get(class_name, class_name)


def inline_summary(
    _context: ExtractionContext, _fired: list[str], pick_count: int
) -> str:
    """Summary for the inline (text + metadata) level — count-driven."""
    if pick_count:
        return f"Found {pick_count} place(s) from the post"
    return "No places found in the text"


def deep_summary(
    _context: ExtractionContext, fired: list[str], _pick_count: int
) -> str:
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
    """Drop the request before Search when producers contributed too
    many names.

    Runs *before* `PlacesSearcher` to gate Google Places quota — every
    `KnownPlace` (and `location_tag`, if set) becomes one Google Places
    Text Search call. Counting names instead of post-pick results lets
    us bail before paying the API cost when a Google Maps shared list
    explodes the candidate set.

    Counts distinct normalized query strings (matching the searcher's
    own dedup), so duplicate names from different producers don't
    inflate the count.
    """
    seen: set[str] = set()
    for kp in context.known_places:
        if kp.name and kp.name.strip():
            seen.add(_query_norm(kp.name))
    if context.location_tag and context.location_tag.strip():
        seen.add(_query_norm(context.location_tag))
    seen.discard("")
    found = len(seen)
    if found <= limit:
        return
    emit(
        "save.cap_exceeded",
        f"Found {found} possible places, more than the limit of {limit} — "
        "skipping this request to protect the system",
    )
    raise TooManyCandidatesError(found=found, limit=limit)


def _query_norm(name: str) -> str:
    import re as _re

    without_punct = _re.sub(r"[^\w\s]", "", name, flags=_re.UNICODE)
    return " ".join(without_punct.lower().split())


class ExtractionPipeline:
    """Search-first level-driven extraction runner.

    A list of `EnrichmentLevel`s is run in order. After each executed
    level the pipeline runs the `searcher` (Google Places fan-out) and
    the `picker` (LLM choice + classification), enforces the cap, and
    returns the picker's output if non-empty. Levels that declare
    `requires_url=True` are skipped silently when the input has no URL.

    Default configuration wires two levels:
    - "enrich"           — TikTok oEmbed + yt-dlp + Google Maps list
                           + photo detector (text + name producers).
    - "deep_enrichment"  — subtitle, whisper, vision (URL-only fallback).
    """

    def __init__(
        self,
        levels: list[EnrichmentLevel],
        searcher: SearcherProtocol,
        picker: PickerProtocol,
        extraction_config: ExtractionConfig,
    ) -> None:
        self._levels = levels
        self._searcher = searcher
        self._picker = picker
        self._extraction_config = extraction_config

    async def run(
        self,
        url: str | None,
        user_id: str,
        limit: int,
        supplementary_text: str = "",
        emit: EmitFn | None = None,
    ) -> list[ValidatedCandidate]:
        """Run the search-first extraction cascade.

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
            executed, fired = await level.run(context)
            if not executed:
                continue

            # 1. Cap — guard Google Places quota before any API calls.
            _enforce_candidate_limit(context, limit, _emit)

            # 2. Search — fan out Google Places Text Search.
            await self._searcher.search(context)

            # 3. Pick — empty search_matches short-circuits before the LLM.
            results = await self._picker.pick(context)

            # 4. Dedup by provider_id and emit.
            level_summary = level.summary_fn(context, fired, len(results))
            _emit(f"save.{level.name}", level_summary)

            if results:
                deduped = dedup_validated_by_provider_id(
                    results, self._extraction_config.confidence
                )
                _emit(
                    "save.validate",
                    f"Confirmed {len(deduped)} place(s) via Places provider",
                )
                return deduped

        _emit("save.validate", "Could not confirm any places")
        return []
