"""Search-first extraction stage — Google Places Text Search fan-out.

`PlacesSearcher` runs after every executed `EnrichmentLevel` and turns
the names producers contributed (`KnownPlace`s + `location_tag`) into a
vetted list of `SearchMatch`es. The picker LLM then chooses from this
closed set — it cannot invent venues. Search is authoritative: any
name that doesn't resolve to a real Google Places hit drops here.

Drops at the searcher boundary:
- `PlacesMatchQuality.NONE` and `CATEGORY_ONLY` — the picker only
  ever sees real matches.
- Geographic-feature `place_types` (route, locality, country, …) —
  Google returns matches for "Bangkok" or "Sukhumvit" that are
  geographic features, not venues; we filter them out so the picker
  isn't tempted to emit them.

Idempotent across levels: a query string already searched on Level 1
is skipped on Level 2. Lets the pipeline re-call `search()` after the
deep level adds vision-detected names without re-querying anything.

`reconcile_picks` joins the picker's `ValidatedCandidate` output back
to `SearchMatch`es by `external_id`. It's the safety net for LLM
transcription drift — if the picker emits an `external_id` not in
the search set, the pick is dropped; for any field the picker echoed
back (name/address/lat/lng), Google's value wins on mismatch.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.confidence import calculate_confidence
from totoro_ai.core.extraction.types import (
    Evidence,
    EvidenceField,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
    SearchMatch,
    ValidatedCandidate,
)
from totoro_ai.core.places import (
    PlaceProvider,
    PlacesClient,
    PlacesMatchQuality,
    PlacesMatchResult,
)

logger = logging.getLogger(__name__)


# Google Places types we always reject — pure administrative or
# infrastructural matches with nothing for a user to "save". Cities
# (locality), countries, and streets are not destinations on their
# own; postal codes / intersections are addressing primitives. We
# DELIBERATELY DO NOT drop `neighborhood`, `sublocality*`, or
# `natural_feature` — those map to real saveable places (the Jordaan
# District, The Nine Streets, Vondelpark-the-park, etc.) even though
# Google tags them with a "geographic" label.
_GEOGRAPHIC_PLACE_TYPES: frozenset[str] = frozenset(
    {
        "route",
        "street_address",
        "locality",
        "country",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "administrative_area_level_4",
        "administrative_area_level_5",
        "postal_code",
        "intersection",
    }
)


# Match-quality → confidence modifier (ADR-029).
_QUALITY_MODIFIERS: dict[PlacesMatchQuality, float] = {
    PlacesMatchQuality.EXACT: 1.0,
    PlacesMatchQuality.FUZZY: 0.9,
    PlacesMatchQuality.CATEGORY_ONLY: 0.8,
    PlacesMatchQuality.NONE: 0.3,
}


# Google's external_provider strings we know how to namespace.
_PROVIDER_MAP: dict[str, PlaceProvider] = {
    "google": PlaceProvider.google,
    "foursquare": PlaceProvider.foursquare,
    "manual": PlaceProvider.manual,
}


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
    # Google Maps shared list; vision-side known_places contribute via
    # the per-search-match evidence join (see `_evidence_for_pick`).
    EvidenceField.KNOWN_PLACES: Medium.LIST,
}


def _normalize(name: str) -> str:
    """Lowercase + strip punctuation. Same shape as `evidence.normalize_name`
    so query-dedup keys are stable across producer types."""
    without_punct = re.sub(r"[^\w\s]", "", name, flags=re.UNICODE)
    return " ".join(without_punct.lower().split())


class PlacesSearcher:
    """Per-level Google Places Text Search fan-out (search step of the
    Search → LLM → Validate → Upsert flow).

    Reads `context.known_places` and `context.location_tag` for queries.
    Runs `PlacesClient.validate_place` in parallel; appends valid
    `SearchMatch`es to `context.search_matches`. Skips queries already
    searched on a previous level (idempotent across the level loop).
    """

    def __init__(self, places_client: PlacesClient) -> None:
        self._places_client = places_client

    async def search(self, context: ExtractionContext) -> None:
        queries = list(self._collect_queries(context))
        if not queries:
            return

        seen_queries = {_normalize(m.query) for m in context.search_matches}
        fresh = [
            (q_norm, query, producer, medium)
            for query, producer, medium in queries
            for q_norm in (_normalize(query),)
            if q_norm and q_norm not in seen_queries
        ]
        if not fresh:
            return

        # Dedup within the new batch — different producers may have
        # emitted the same name; one Google call covers both.
        first_seen: dict[str, tuple[str, Producer, Medium]] = {}
        for q_norm, query, producer, medium in fresh:
            if q_norm not in first_seen:
                first_seen[q_norm] = (query, producer, medium)

        location_hint = context.location_tag
        results = await asyncio.gather(
            *(
                self._search_one(query, producer, medium, location_hint)
                for query, producer, medium in first_seen.values()
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, SearchMatch):
                context.search_matches.append(result)

    async def _search_one(
        self,
        query: str,
        producer: Producer,
        medium: Medium,
        location_hint: str | None,
    ) -> SearchMatch | None:
        try:
            match: PlacesMatchResult = await self._places_client.validate_place(
                name=query, location=location_hint
            )
        except Exception as exc:  # noqa: BLE001 — best-effort per query
            logger.warning(
                "places_search_failed",
                extra={"query": query, "error": str(exc)},
            )
            return None

        if match.match_quality not in (
            PlacesMatchQuality.EXACT,
            PlacesMatchQuality.FUZZY,
        ):
            return None
        if match.external_id is None:
            return None
        if _GEOGRAPHIC_PLACE_TYPES.intersection(match.place_types):
            return None

        provider = _PROVIDER_MAP.get(
            (match.external_provider or "").lower(), PlaceProvider.google
        )
        return SearchMatch(
            query=query,
            query_producer=producer,
            query_medium=medium,
            validated_name=match.validated_name or query,
            provider=provider,
            external_id=match.external_id,
            match_quality=match.match_quality,
            lat=match.lat,
            lng=match.lng,
            address=match.address,
            place_types=tuple(match.place_types),
        )

    @staticmethod
    def _collect_queries(
        context: ExtractionContext,
    ) -> Iterable[tuple[str, Producer, Medium]]:
        """Yield (query, producer, medium) tuples in priority order.

        Producer/medium tags follow the `KnownPlace` that contributed
        the name so the resulting `SearchMatch` carries provenance for
        evidence assembly downstream. `location_tag` rides as
        `VIDEO_METADATA + LOCATION_TAG` — that's the only producer that
        populates it today.
        """
        for kp in context.known_places:
            if kp.name and kp.name.strip():
                yield kp.name.strip(), kp.producer, kp.medium
        if context.location_tag and context.location_tag.strip():
            yield (
                context.location_tag.strip(),
                Producer.VIDEO_METADATA,
                Medium.LOCATION_TAG,
            )


def reconcile_picks(
    picks: list[ValidatedCandidate],
    search_matches: list[SearchMatch],
    confidence_config: ConfidenceConfig,
    context: ExtractionContext,
) -> list[ValidatedCandidate]:
    """Cross-check picker output against search_matches; rebuild
    canonical fields and confidence.

    Defense in depth — instructor's enum binding on `external_id`
    should already prevent out-of-set IDs, but if one slips through we
    drop the pick rather than letting it reach persistence. For every
    other field (`place_name` / `match_address` / `match_lat` /
    `match_lng` / `provider`), Google is canonical: we always source
    these from the matching `SearchMatch` and discard whatever value
    the picker emitted, even on transcription drift. Confidence is
    recomputed via `calculate_confidence` using the Google match-
    quality modifier; the picker's `confidence` field is ignored.
    """
    if not picks:
        return []

    by_id: dict[str, SearchMatch] = {m.external_id: m for m in search_matches}
    out: list[ValidatedCandidate] = []
    for pick in picks:
        match = by_id.get(pick.external_id)
        if match is None:
            logger.warning(
                "pick_missing_from_search_matches",
                extra={
                    "external_id": pick.external_id,
                    "place_name": pick.place_name,
                },
            )
            continue

        evidence = _evidence_for_pick(pick.evidence, match, context)
        modifier = _QUALITY_MODIFIERS[match.match_quality]
        confidence = calculate_confidence(
            evidence=evidence,
            match_modifier=modifier,
            config=confidence_config,
        )
        if confidence == 0.0:
            continue

        out.append(
            ValidatedCandidate(
                place_name=match.validated_name,
                place_type=pick.place_type,
                provider=match.provider,
                external_id=match.external_id,
                confidence=confidence,
                evidence=evidence,
                subcategory=pick.subcategory,
                tags=pick.tags,
                attributes=pick.attributes,
                match_lat=match.lat,
                match_lng=match.lng,
                match_address=match.address,
            )
        )
    return out


def _evidence_for_pick(
    picker_evidence: list[Evidence],
    match: SearchMatch,
    context: ExtractionContext,
) -> list[Evidence]:
    """Build the final Evidence list for one pick.

    Combines:
    1. `picker_evidence` — what the picker self-reported as supporting
       fields (already converted to `Evidence(LLM_NER, medium)` items
       by the caller in `llm_picker.py`).
    2. The search-side producer that contributed the query name
       (`SearchMatch.query_producer + query_medium`) — preserves "this
       match came from a Google Maps shared list" / "this came from
       vision frames" attribution even when the picker's text-side
       evidence misses the producer chain.
    3. Upstream `text_evidence` — yt-dlp / oEmbed / etc. populating
       caption/transcript/title/hashtag fields the picker actually
       used. Joined by Medium so a yt-dlp `caption` evidence rides
       alongside the picker's `LLM_NER + caption` evidence whenever
       the picker reported `EvidenceField.CAPTION`.
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

    # Search-side producer chain — always present, names which producer
    # surfaced this venue in the first place.
    _push(
        Evidence(
            producer=match.query_producer,
            medium=match.query_medium,
            snippet=match.query,
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
    query_norm = _normalize(match.query)
    for kp in context.known_places:
        if _normalize(kp.name) == query_norm:
            _push(_known_place_evidence(kp))

    return out


def _known_place_evidence(kp: KnownPlace) -> Evidence:
    return Evidence(producer=kp.producer, medium=kp.medium, snippet=kp.snippet)


def evidence_field_to_medium(field: EvidenceField) -> Medium:
    """Map a picker-reported EvidenceField onto a Medium for evidence assembly."""
    return _EVIDENCE_FIELD_TO_MEDIUM[field]
