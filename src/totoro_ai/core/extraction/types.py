"""Shared types for the extraction cascade.

`CandidatePlace` and `ValidatedCandidate` carry only the fields the
extraction pipeline can derive from source content (caption, transcript,
images) plus the cascade metadata the validator and dedup passes need.
Pipeline-context fields (`user_id`, `source_url`, `source`) and
persistence concerns are NOT on these types — they live on
`ExtractionContext` and are stamped onto a `PlaceCreate` only at the
persistence boundary, which is the single point that needs the full
write-side shape.

`PlaceCreate` and `PlaceObject` are re-exported so callers that imported
them from this module continue to resolve during the migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from totoro_ai.core.places import (
    PlaceAttributes,
    PlaceCreate,
    PlaceObject,
    PlaceProvider,
    PlaceSource,
    PlaceType,
)

__all__ = [
    "ExtractionLevel",
    "CandidatePlace",
    "ExtractionContext",
    "ValidatedCandidate",
    # Re-exported from core.places for legacy import paths.
    "PlaceCreate",
    "PlaceObject",
]


class ExtractionLevel(Enum):
    """Enricher levels that produce CandidatePlace objects."""

    EMOJI_REGEX = "emoji_regex"
    LLM_NER = "llm_ner"
    SUBTITLE_CHECK = "subtitle_check"
    WHISPER_AUDIO = "whisper_audio"
    VISION_FRAMES = "vision_frames"
    GOOGLE_MAPS_LIST = "google_maps_list"


@dataclass
class CandidatePlace:
    """Pre-validation extraction candidate.

    Holds only the fields the producing enricher could derive from the
    source content. No `user_id` (that's pipeline context), no
    `provider` / `external_id` (the validator fills those in on
    `ValidatedCandidate`), no `source_url` / `source` (stamped at
    persistence time). Mutable so dedup can merge attributes in place.
    """

    place_name: str
    place_type: PlaceType
    source: ExtractionLevel
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)
    corroborated: bool = False
    signals: list[str] = field(default_factory=list)


@dataclass
class ExtractionContext:
    """Shared mutable state threaded through all enrichers.

    `source` is auto-derived from `url` in `__post_init__` so every
    consumer (enrichers, persistence, the service) reads the same
    canonical `PlaceSource` without re-parsing the URL. Callers may pass
    `source` explicitly to override (e.g. tests).

    `known_places` is a list of confirmed place names from external
    sources where the system already knows the place exists (e.g. a
    Google Maps shared list pulled via Apify). Producers append names
    here instead of building `CandidatePlace`s directly; the NER
    finalizer reads this list as one of its text sources and emits
    structured candidates with inferred attributes — same path as any
    other text-derived candidate.
    """

    url: str | None
    user_id: str
    supplementary_text: str = ""
    caption: str | None = None
    transcript: str | None = None
    candidates: list[CandidatePlace] = field(default_factory=list)
    platform: str | None = None
    title: str | None = None
    hashtags: list[str] = field(default_factory=list)
    location_tag: str | None = None
    source: PlaceSource | None = None
    known_places: list[str] = field(default_factory=list)
    is_photo_post: bool = False
    image_urls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source is None:
            from totoro_ai.core.extraction.url_source import source_from_url

            self.source = source_from_url(self.url)


@dataclass
class ValidatedCandidate:
    """Post-validation extraction candidate.

    The validator stamps `provider` + `external_id` (resolved from Google
    Places) onto the candidate fields and adds `confidence` /
    `resolved_by` / corroboration metadata. `match_lat` / `match_lng` /
    `match_address` carry the Tier 2 geo data Google Places returned so
    the persistence layer can write it to `PlacesCache` after the Tier
    1 row is created. All three geo fields are optional — `None` when
    Google returned NONE-quality or the validator was bypassed.
    """

    place_name: str
    place_type: PlaceType
    provider: PlaceProvider
    external_id: str
    confidence: float
    resolved_by: ExtractionLevel
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)
    corroborated: bool = False
    match_lat: float | None = None
    match_lng: float | None = None
    match_address: str | None = None
