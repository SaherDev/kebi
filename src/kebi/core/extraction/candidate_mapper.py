"""Bridges between PlaceObject (v2) and extraction-internal candidate shapes.

The pipeline produces v2 `PlaceObject` instances from
`PlacesSearchService.find()` (search-result side) and v2 `PlaceCore`
instances heading into `PlaceUpsertService.upsert_and_embed`
(persistence side). The LLM picker emits a slim picker-internal shape
(`ValidatedCandidate`) that has to be projected from one and into the
other.

This module also owns `reconcile_picks` (cross-checks the picker's
output against the search-result set; recomputes confidence) and the
`evidence_field_to_medium` helper. Both used to live in
`core/extraction/searcher.py`; they survive that file's deletion here
because the pipeline + picker still need them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.confidence import calculate_confidence
from kebi.core.extraction.types import (
    Evidence,
    EvidenceField,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PickerCandidateInput:
    """Slim projection of a v2 `PlaceObject` for the LLM picker prompt.

    The picker consumes this — not the full `PlaceObject` — so the
    prompt template stays compact and Google's live fields
    (rating / hours / phone / popularity / business_status) don't leak
    into the LLM context.

    The picker echoes `provider_id` back on its output; the rest of
    the picked place comes from the original `PlaceObject` via the
    `search_set` lookup in `reconcile_picks`.
    """

    provider_id: str
    place_name: str
    location_summary: str  # e.g. "Bangkok, Thailand — Sukhumvit Soi 11"
    categories: tuple[PlaceCategory, ...]


@dataclass(frozen=True)
class AttributedSearchResult:
    """A v2 `PlaceObject` plus the producer-side attribution that surfaced it.

    The pipeline calls `PlacesSearchService.find()` once per
    `KnownPlace` (or for the request's `location_tag`). The
    `query_producer`/`query_medium`/`query` triple captures which
    enricher contributed the query that found this place, so the
    evidence trail can record where each pick originated.
    """

    place: PlaceObject
    query: str
    query_producer: Producer
    query_medium: Medium


def search_results_to_picker_input(
    results: list[AttributedSearchResult],
) -> list[PickerCandidateInput]:
    """Project attributed results into the slim shape the picker prompt consumes.

    Drops anything without a `provider_id` (defensive — should not
    happen because `PlacesSearchService` always namespaces).
    """
    rows: list[PickerCandidateInput] = []
    for ar in results:
        p = ar.place
        if p.provider_id is None:
            continue
        rows.append(
            PickerCandidateInput(
                provider_id=p.provider_id,
                place_name=p.place_name,
                location_summary=_format_location(p.location),
                categories=tuple(p.categories),
            )
        )
    return rows


def candidate_to_core(c: ValidatedCandidate) -> PlaceCore:
    """Build a v2 `PlaceCore` from a `ValidatedCandidate` for upsert.

    Called once per saved candidate at the persistence boundary inside
    `ExtractionService.run`.
    """
    return PlaceCore(
        provider_id=c.provider_id,
        place_name=c.place_name,
        categories=c.categories,
        tags=c.tags,
        location=c.location,
    )


def evidence_field_to_medium(field: EvidenceField) -> Medium:
    """Map a picker-reported EvidenceField onto a Medium for evidence assembly."""
    return _EVIDENCE_FIELD_TO_MEDIUM[field]


# Map the picker's self-reported `EvidenceField`s onto their `Medium`
# counterparts so synthesized `Evidence` records line up with the
# medium_scores config.
_EVIDENCE_FIELD_TO_MEDIUM: dict[EvidenceField, Medium] = {
    EvidenceField.CAPTION: Medium.CAPTION,
    EvidenceField.TRANSCRIPT: Medium.TRANSCRIPT,
    EvidenceField.TITLE: Medium.TITLE,
    EvidenceField.HASHTAG: Medium.HASHTAG,
    EvidenceField.LOCATION_TAG: Medium.LOCATION_TAG,
    EvidenceField.SUPPLEMENTARY_TEXT: Medium.SUPPLEMENTARY_TEXT,
    # KNOWN_PLACES has no direct text Medium — it's the producer-side
    # signal. Synthesize as `LIST` since the canonical example is the
    # Google Maps shared list.
    EvidenceField.KNOWN_PLACES: Medium.LIST,
}


# Without Google's raw match_quality (filtered out by PlacesSearchService),
# every result we see has already passed the v2 service's vetting.
# Treat them all as the equivalent of legacy EXACT — modifier 1.0. The
# legacy modifier-by-quality logic (ADR-029) is no longer load-bearing
# because the v2 service only returns hits, not classification-only matches.
_V2_MATCH_MODIFIER = 1.0


def reconcile_picks(
    picks: list[ValidatedCandidate],
    search_set: dict[str, AttributedSearchResult],
    confidence_config: ConfidenceConfig,
    context: ExtractionContext,
) -> list[ValidatedCandidate]:
    """Cross-check picker output against the search-result set, rebuild
    canonical fields, recompute confidence.

    Defense in depth — Instructor's enum/str validation should already
    keep `provider_id` inside the set, but if a stray pick slips
    through, drop it rather than letting it reach persistence. For
    every other field (`place_name`, `location`), the v2 `PlaceObject`
    is canonical: we always source these from the matching search
    result and discard whatever value the picker emitted. Confidence
    is recomputed via `calculate_confidence` using
    `_V2_MATCH_MODIFIER=1.0` (the v2 service has already filtered to
    real hits — quality classification is no longer surfaced).
    """
    if not picks:
        return []

    out: list[ValidatedCandidate] = []
    for pick in picks:
        attributed = search_set.get(pick.provider_id)
        if attributed is None:
            logger.warning(
                "pick_missing_from_search_set",
                extra={
                    "provider_id": pick.provider_id,
                    "place_name": pick.place_name,
                },
            )
            continue

        evidence = _evidence_for_pick(pick.evidence, attributed, context)
        confidence = calculate_confidence(
            evidence=evidence,
            match_modifier=_V2_MATCH_MODIFIER,
            config=confidence_config,
        )
        if confidence == 0.0:
            continue

        place = attributed.place
        out.append(
            ValidatedCandidate(
                place_name=place.place_name,
                provider_id=place.provider_id or pick.provider_id,
                categories=pick.categories,
                tags=pick.tags,
                confidence=confidence,
                evidence=evidence,
                subcategory=pick.subcategory,
                location=place.location,
            )
        )
    return out


def _evidence_for_pick(
    picker_evidence: list[Evidence],
    attributed: AttributedSearchResult,
    context: ExtractionContext,
) -> list[Evidence]:
    """Build the final Evidence list for one pick.

    Combines:
    1. `picker_evidence` — what the picker self-reported as supporting
       fields (already converted to `Evidence(LLM_NER, medium)` items
       by the caller in `llm_picker.py`).
    2. The search-side producer that contributed the query name
       (`AttributedSearchResult.query_producer + query_medium`) —
       preserves "this match came from a Google Maps shared list" /
       "this came from vision frames" attribution even when the
       picker's text-side evidence misses the producer chain.
    3. Upstream `text_evidence` — yt-dlp / oEmbed / etc. populating
       caption/transcript/title/hashtag fields the picker actually used.
    4. Matching `KnownPlace` entries — when the search match's query
       came from one of them, attach as `Evidence(producer, medium,
       snippet)` so the producer chain stays intact.
    """
    seen: set[Evidence] = set()
    out: list[Evidence] = []

    def _push(item: Evidence) -> None:
        if item not in seen:
            seen.add(item)
            out.append(item)

    for ev in picker_evidence:
        _push(ev)

    # Search-side producer chain — always present.
    _push(
        Evidence(
            producer=attributed.query_producer,
            medium=attributed.query_medium,
            snippet=attributed.query,
        )
    )

    # Upstream text producers whose source field was used by the picker.
    picker_media = {ev.medium for ev in picker_evidence}
    for te in context.text_evidence:
        if te.medium in picker_media:
            _push(te)

    # Matching KnownPlace entries — vision-frame / vision-image / list
    # signals all live as KnownPlaces; the search match's query is the
    # name they contributed.
    query_norm = normalize_query(attributed.query)
    for kp in context.known_places:
        if normalize_query(kp.name) == query_norm:
            _push(_known_place_evidence(kp))

    return out


def _known_place_evidence(kp: KnownPlace) -> Evidence:
    return Evidence(producer=kp.producer, medium=kp.medium, snippet=kp.snippet)


def normalize_query(name: str) -> str:
    """Lowercase + strip punctuation. Stable shape used for dedup +
    KnownPlace match-join. Public because the pipeline-side
    deduplication of search results uses the same function."""
    without_punct = re.sub(r"[^\w\s]", "", name, flags=re.UNICODE)
    return " ".join(without_punct.lower().split())


def _format_location(loc: LocationContext | None) -> str:
    if loc is None:
        return ""
    parts = [loc.neighborhood, loc.city, loc.country]
    return ", ".join(p for p in parts if p)
