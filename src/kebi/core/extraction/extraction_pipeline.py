"""ExtractionPipeline — level-by-level runner for the search-first cascade.

Search-first flow (one pass per `EnrichmentLevel`):

1. **Producers** populate text fields (caption / transcript / title /
   hashtags / location_tag) and `KnownPlace`s (Google Maps shared
   list, vision frames, vision images).
2. **Cap** (`_enforce_candidate_limit`) drops the request when the
   number of unique candidate names exceeds the configured ceiling.
3. **Search** — for each unique `KnownPlace` name (plus `location_tag`
   when present), call `PlacesSearchService.find()` and collect the
   results into a `search_set` keyed by `provider_id`. Geographic
   features (administrative-only names like districts / roads) are
   dropped via `drop_geographic_features`.
4. **Pick** (`LLMPlacePicker`) lets the LLM choose which results the
   post actually references and infer the v2 vocabulary
   (`categories`, `tags`).
5. **Dedup** (`dedup_by_provider_id`) collapses picks that share a
   `provider_id` (when two levels happen to surface the same place).

The first level whose pick set is non-empty short-circuits and
returns; subsequent levels (e.g. deep enrichment with subtitle /
whisper / vision) only run when earlier levels produced nothing.

Per ADR-070, `places_v2.PlacesSearchService` is the single source of
truth for place lookups. Extraction never calls Google directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from kebi.core.config import ExtractionConfig
from kebi.core.emit import EmitFn
from kebi.core.extraction.candidate_mapper import (
    AttributedSearchResult,
    normalize_query,
)
from kebi.core.extraction.dedup import dedup_by_provider_id
from kebi.core.extraction.enrichment_level import EnrichmentLevel
from kebi.core.extraction.geo_filter import drop_geographic_features
from kebi.core.extraction.types import (
    ExtractionContext,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places_v2 import (
    LocationContext,
    PlaceObject,
    PlaceQuery,
    PlacesSearchServiceProtocol,
)

logger = logging.getLogger(__name__)


class PickerProtocol(Protocol):
    """Pick-step contract — LLM picks from the search_set, returns
    `ValidatedCandidate`s."""

    async def pick(
        self,
        context: ExtractionContext,
        search_set: dict[str, AttributedSearchResult],
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
    returns a `failed` envelope with a user-facing reason.
    """

    def __init__(self, found: int, limit: int) -> None:
        super().__init__(f"Found {found} candidates; limit is {limit}")
        self.found = found
        self.limit = limit


def _enforce_candidate_limit(
    context: ExtractionContext, limit: int, emit: EmitFn
) -> None:
    """Drop the request before search when producers contributed too
    many names. Counts distinct normalized query strings so duplicate
    names from different producers don't inflate the count."""
    seen: set[str] = set()
    for kp in context.known_places:
        if kp.name and kp.name.strip():
            seen.add(normalize_query(kp.name))
    if context.location_tag and context.location_tag.strip():
        seen.add(normalize_query(context.location_tag))
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


# Per-query result limit handed to PlacesSearchService.find(). 5 is
# generous enough that the picker has real choice for any single name
# (typo, ambiguous brand, etc.) and tight enough that quota stays sane
# under many-name requests like a Google Maps shared list.
_SEARCH_LIMIT_PER_QUERY = 5
# Cap on parallel PlacesSearchService.find() calls across producer names.
# Bounds DB/cache/provider QPS the way the places_v2 client's own fan-out
# semaphores do, while collapsing the N-name latency to ~one call.
_SEARCH_CONCURRENCY = 5


class ExtractionPipeline:
    """Search-first level-driven extraction runner.

    A list of `EnrichmentLevel`s is run in order. After each executed
    level the pipeline fans out `PlacesSearchService.find()` over the
    producers' contributed names, applies the geo-feature filter, and
    hands the result set to the LLM picker. Levels that declare
    `requires_url=True` are skipped silently when the input has no URL.

    Default configuration wires two levels:
    - "enrich"           — TikTok oEmbed + yt-dlp + Google Maps list
                           + Instagram + TikTok photo (text + name producers).
    - "deep_enrichment"  — subtitle, whisper, vision (URL-only fallback).
    """

    def __init__(
        self,
        levels: list[EnrichmentLevel],
        search_service: PlacesSearchServiceProtocol,
        picker: PickerProtocol,
        extraction_config: ExtractionConfig,
    ) -> None:
        self._levels = levels
        self._search_service = search_service
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

        `limit` is the per-request candidate cap (counted on names,
        not search results). The pipeline takes a concrete value;
        default-fallback logic lives in `ExtractionService.run`.
        """
        _emit: EmitFn = emit or (lambda step, summary, duration_ms=None: None)

        context = ExtractionContext(
            url=url,
            user_id=user_id,
            supplementary_text=supplementary_text,
        )

        # Search-set accumulates across levels (idempotent — same
        # provider_id from a later level overwrites without re-querying).
        search_set: dict[str, AttributedSearchResult] = {}
        searched_queries: set[str] = set()

        for level in self._levels:
            executed, fired = await level.run(context)
            if not executed:
                continue

            _enforce_candidate_limit(context, limit, _emit)

            await self._extend_search_set(context, search_set, searched_queries)

            results = await self._picker.pick(context, search_set)

            level_summary = level.summary_fn(context, fired, len(results))
            _emit(f"save.{level.name}", level_summary)

            if results:
                deduped = dedup_by_provider_id(
                    results, self._extraction_config.confidence
                )
                _emit(
                    "save.validate",
                    f"Confirmed {len(deduped)} place(s) via Places provider",
                )
                return deduped

        _emit("save.validate", "Could not confirm any places")
        return []

    async def _extend_search_set(
        self,
        context: ExtractionContext,
        search_set: dict[str, AttributedSearchResult],
        searched_queries: set[str],
    ) -> None:
        """Fan out `PlacesSearchService.find()` over the new producer
        names (skip queries already issued on an earlier level). Drop
        geographic-feature results and attribute each surviving result
        to the `KnownPlace` whose name produced it.

        Cross-name dedup: when two different producers contributed the
        same name (normalized), one search call covers both. The result
        is attributed to whichever producer was seen first.
        """
        queries: list[tuple[str, Producer, Medium]] = []

        seen_in_batch: set[str] = set()
        for kp in context.known_places:
            if not kp.name or not kp.name.strip():
                continue
            q_norm = normalize_query(kp.name)
            if not q_norm or q_norm in searched_queries or q_norm in seen_in_batch:
                continue
            seen_in_batch.add(q_norm)
            queries.append((kp.name.strip(), kp.producer, kp.medium))

        if context.location_tag and context.location_tag.strip():
            q_norm = normalize_query(context.location_tag)
            already_searched = q_norm in searched_queries or q_norm in seen_in_batch
            if q_norm and not already_searched:
                seen_in_batch.add(q_norm)
                queries.append(
                    (
                        context.location_tag.strip(),
                        Producer.VIDEO_METADATA,
                        Medium.LOCATION_TAG,
                    )
                )

        if not queries:
            return

        location_hint = _location_hint_from(context)

        sem = asyncio.Semaphore(_SEARCH_CONCURRENCY)

        async def _find(query: str) -> list[PlaceObject]:
            """Run one name's search; best-effort — errors degrade to []."""
            try:
                async with sem:
                    return await self._search_service.find(
                        PlaceQuery(place_names=[query], location=location_hint),
                        limit=_SEARCH_LIMIT_PER_QUERY,
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort per query
                logger.warning(
                    "places_search_failed",
                    extra={"query": query, "error": str(exc)},
                )
                return []

        batches = await asyncio.gather(*[_find(q) for q, _, _ in queries])

        # Merge in `queries` order (not completion order) so cross-name dedup
        # stays deterministic — first producer to claim a provider_id wins.
        for (query, producer, medium), results in zip(
            queries, batches, strict=True
        ):
            for place in drop_geographic_features(results):
                if not place.provider_id:
                    continue
                if place.provider_id in search_set:
                    continue
                search_set[place.provider_id] = AttributedSearchResult(
                    place=place,
                    query=query,
                    query_producer=producer,
                    query_medium=medium,
                )
            searched_queries.add(normalize_query(query))


def _location_hint_from(context: ExtractionContext) -> LocationContext | None:
    """If the post has a location_tag, surface it to the search service
    as a hint. v2 `PlaceQuery.location` accepts a `LocationContext`;
    extraction only knows the tag string so the hint is name-shaped.
    None means no hint (let the search run unrestricted)."""
    if not context.location_tag or not context.location_tag.strip():
        return None
    # The tag is unstructured ("Bangkok", "Sukhumvit", …). Surface it
    # via the `address` field — closest match for a free-text place hint.
    return LocationContext(address=context.location_tag.strip())


__all__ = [
    "ExtractionPipeline",
    "PickerProtocol",
    "TooManyCandidatesError",
    "inline_summary",
    "deep_summary",
]
