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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

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
    PlaceNameAlias,
    PlaceObject,
    PlaceTag,
    TagType,
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
    # The string actually sent to the provider text search. Equals
    # `query` for the raw-name path; differs when the resolver cleaned
    # the name (ADR-080). `query` stays the raw KnownPlace name so the
    # `_evidence_for_pick` normalize-join against `context.known_places`
    # remains stable.
    search_query: str = ""
    # The clean human label the user saw in the post (resolver-LLM
    # produced — ADR-081), distinct from both the canonical name and
    # the search query. Empty ⇒ fall back to `query`.
    display_label: str = ""


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


def candidate_to_core(
    c: ValidatedCandidate,
    aliases: list[PlaceNameAlias] | None = None,
) -> PlaceCore:
    """Build a v2 `PlaceCore` from a `ValidatedCandidate` for upsert.

    Called once per saved candidate at the persistence boundary inside
    `ExtractionService.run`. `aliases` is the optional shared
    `place_name_aliases` contribution — the caller owns the
    confidence gate and source attribution (ADR-081), so this stays a
    pure constructor. The upsert merge dedups by value / existing-wins.
    """
    return PlaceCore(
        provider_id=c.provider_id,
        place_name=c.place_name,
        place_name_aliases=aliases or [],
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
        # The raw producer label the user saw in the post (e.g. a
        # TikTok card title), kept only when it differs from the
        # canonical name — that difference is the whole point of
        # showing the user the name they know it by.
        # Resolver-cleaned display label (ADR-081); fall back to the
        # raw producer name only if the resolver produced none.
        label = (attributed.display_label or attributed.query).strip()
        source_label = (
            label
            if label and normalize_query(label) != normalize_query(place.place_name)
            else None
        )
        out.append(
            ValidatedCandidate(
                place_name=place.place_name,
                provider_id=place.provider_id or pick.provider_id,
                # The picker's categories win, but it's told to emit none when a
                # candidate has no obvious category — which blanks places Google
                # did classify (e.g. a temple → `temple`). Fall back to the v2
                # PlaceObject's Google-mapped categories so those don't end up
                # uncategorised. Both are already strict PlaceCategory values.
                categories=pick.categories or place.categories,
                tags=pick.tags,
                confidence=confidence,
                evidence=evidence,
                subcategory=pick.subcategory,
                location=place.location,
                source_label=source_label,
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


@dataclass(frozen=True)
class ResolverOutput:
    """Pre-search resolver result (ADR-080, ADR-082).

    `queries` maps `normalize_query(raw KnownPlace name)` → the cleaned
    string to send to provider text search. A raw name absent from the
    map was dropped as non-place noise and must not be searched.
    `location` is the shared location context inferred for the whole
    post — the default search bias. `query_locations` (ADR-082) holds
    per-candidate location overrides for multi-destination posts: a
    candidate keyed there is searched with ITS own location instead of
    the shared one; candidates absent from it fall back to `location`.
    `post_tags` are shared post-level attribute tags merged into every
    pick.
    """

    queries: dict[str, str] = field(default_factory=dict)
    # `normalize_query(raw KnownPlace name)` → the clean human label
    # the user saw in the post (list numbering / decorations / emoji
    # stripped by the resolver LLM, NOT swapped for the canonical or
    # search name). Drives `user_places.source_label` + shared aliases
    # (ADR-081). Absent ⇒ fall back to the raw name.
    display_labels: dict[str, str] = field(default_factory=dict)
    location: LocationContext | None = None
    # `normalize_query(raw KnownPlace name)` → that candidate's own
    # location (ADR-082). Sparse — only multi-destination posts
    # populate it; absent ⇒ the shared `location` is used.
    query_locations: dict[str, LocationContext] = field(default_factory=dict)
    post_tags: list[PlaceTag] = field(default_factory=list)


class _LLMTagLike(Protocol):
    type: str
    value: str


def llm_tags_to_place_tags(tags: Iterable[_LLMTagLike]) -> list[PlaceTag]:
    """Convert flat LLM-emitted tags → `PlaceTag` with `source="llm"`.

    Shared by the resolver (post-level tags) and the classifier
    (per-place tags). Type values outside `TagType` fall through as
    plain strings — `PlaceTag.type` accepts `TagType | str`. Empty
    type/value pairs are skipped.
    """
    out: list[PlaceTag] = []
    for t in tags:
        if not t.value or not t.type:
            continue
        try:
            tag_type: TagType | str = TagType(t.type)
        except ValueError:
            tag_type = t.type
        out.append(PlaceTag(type=tag_type, value=t.value, source="llm"))
    return out


def merge_tags(per_place: list[PlaceTag], shared: list[PlaceTag]) -> list[PlaceTag]:
    """Union per-place tags with shared post-level tags (ADR-080).

    Dedupe by `(type, value)`; the per-place tag wins on conflict
    (its evidence is venue-specific). Order: per-place first, then any
    shared tag whose `(type, value)` is not already present.
    """

    def _key(tag: PlaceTag) -> tuple[str, str]:
        type_str = tag.type.value if isinstance(tag.type, TagType) else tag.type
        return (type_str, str(tag.value))

    seen = {_key(t) for t in per_place}
    out = list(per_place)
    for t in shared:
        if _key(t) not in seen:
            seen.add(_key(t))
            out.append(t)
    return out


def location_hint_from(context: ExtractionContext) -> LocationContext | None:
    """Degraded location hint from the post's `location_tag`.

    The resolver (ADR-080) normally supplies the shared location; this
    is the fallback used when the resolver call fails or no resolver is
    wired. The tag is unstructured ("Bangkok", "Sukhumvit", …) so it is
    surfaced via `address` — the closest match for a free-text hint.
    `None` means no hint (search runs unrestricted).
    """
    if not context.location_tag or not context.location_tag.strip():
        return None
    return LocationContext(address=context.location_tag.strip())
