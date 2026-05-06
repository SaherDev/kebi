"""Shared types for the extraction cascade.

Every candidate place carries an `evidence` list — one entry per
producer/medium pair that contributed to it. The list is the audit
trail: which enricher saw what, in which slice of pipeline state, with
the actual content snippet when available. `Producer` identifies *who*
contributed; `Medium` identifies *where* in pipeline state the evidence
lived. Together they form `Evidence(producer, medium, snippet, metadata)`.

Pipeline-context fields (`user_id`, `source_url`, `source`) and
persistence concerns are NOT on these types — they live on
`ExtractionContext` and are stamped onto a `PlaceCreate` only at the
persistence boundary.

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
    PlacesMatchQuality,
    PlaceSource,
    PlaceType,
)

__all__ = [
    "Producer",
    "Medium",
    "EvidenceField",
    "Evidence",
    "KnownPlace",
    "SearchMatch",
    "ExtractionContext",
    "ValidatedCandidate",
    # Re-exported from core.places for legacy import paths.
    "PlaceCreate",
    "PlaceObject",
]


class Producer(Enum):
    """Enricher identity for evidence attribution.

    Name producers emit candidate names directly; text producers
    populate fields the NER finalizer reads. Both axes appear in
    `Evidence.producer` so a candidate's audit trail captures every
    enricher that contributed.
    """

    # Name producers — emit candidate names
    LLM_NER = "llm_ner"
    GOOGLE_MAPS_LIST = "google_maps_list"
    INSTAGRAM_POST = "instagram_post"
    VISION_FRAMES = "vision_frames"
    VISION_IMAGES = "vision_images"
    # Text producers — populate caption / transcript / etc.
    TIKTOK_CAPTION = "tiktok_caption"
    VIDEO_METADATA = "video_metadata"
    WHISPER_AUDIO = "whisper_audio"
    SUBTITLE_CHECK = "subtitle_check"
    PHOTO_DETECTOR = "photo_detector"


class Medium(Enum):
    """Where in pipeline state the evidence lived."""

    CAPTION = "caption"
    SUPPLEMENTARY_TEXT = "supplementary_text"
    TRANSCRIPT = "transcript"
    TITLE = "title"
    HASHTAG = "hashtag"
    LOCATION_TAG = "location_tag"
    EMOJI_MARKER = "emoji_marker"
    FRAME = "frame"
    IMAGE = "image"
    LIST = "list"


class EvidenceField(str, Enum):
    """LLM-side label for which text source supported a pick.

    The picker self-reports which fields it leaned on for each chosen
    place. The pipeline maps these to `Evidence(producer=LLM_NER,
    medium=...)` records. Maps onto `Medium` 1:1 for the text fields
    the picker can actually see in its prompt.
    """

    CAPTION = "caption"
    TRANSCRIPT = "transcript"
    TITLE = "title"
    HASHTAG = "hashtag"
    LOCATION_TAG = "location_tag"
    SUPPLEMENTARY_TEXT = "supplementary_text"
    KNOWN_PLACES = "known_places"


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence backing a candidate.

    Frozen so identical evidence items hash equal — dedup-merge can
    union evidence lists without duplicate entries via set semantics.
    `metadata` is a tuple of (key, value) pairs (not a dict) for
    hashability; convert to a dict at the API boundary.
    """

    producer: Producer
    medium: Medium
    snippet: str | None = None
    metadata: tuple[tuple[str, str | int | float | bool], ...] = ()


@dataclass
class KnownPlace:
    """A confirmed venue name produced by a name producer.

    Carries the `producer` + `medium` + `snippet` so the NER finalizer
    can stamp matching candidates with full Evidence — without this,
    every candidate built from `known_places` would look like it came
    only from `LLM_NER`.
    """

    name: str
    producer: Producer
    medium: Medium
    snippet: str | None = None


@dataclass(frozen=True)
class SearchMatch:
    """A vetted Google Places hit that the picker LLM may choose from.

    Produced by `PlacesSearcher` from the names that producers
    contributed (`KnownPlace`s + `location_tag`). Drops NONE /
    CATEGORY_ONLY quality and geographic-feature place_types at the
    searcher boundary — the picker only ever sees real venues.

    Frozen so the searcher's per-query dedup can use set semantics on
    `external_id`. The picker references entries by `external_id` only;
    the rest of the fields are echoed back to the caller as authoritative
    Google data.
    """

    query: str
    query_producer: Producer
    query_medium: Medium
    validated_name: str
    provider: PlaceProvider
    external_id: str
    match_quality: PlacesMatchQuality
    lat: float | None = None
    lng: float | None = None
    address: str | None = None
    place_types: tuple[str, ...] = ()


@dataclass
class ExtractionContext:
    """Shared mutable state threaded through all enrichers and the picker.

    `source` is auto-derived from `url` in `__post_init__` so every
    consumer (enrichers, persistence, the service) reads the same
    canonical `PlaceSource` without re-parsing the URL. Callers may pass
    `source` explicitly to override (e.g. tests).

    `known_places` is a list of `KnownPlace` entries — confirmed venue
    names from name producers (Google Maps shared list, vision frames,
    vision images). Each entry carries the `producer + medium + snippet`
    so the picker can stamp matching picks with full provenance.

    `text_evidence` is a list of `Evidence` entries appended by text
    producers when they actually wrote pipeline state (caption,
    transcript, title, etc.). Used by `assemble_evidence` to attach
    upstream producer attribution (e.g. "this caption came from yt-dlp
    metadata") to whichever places the picker chose.

    `search_matches` is the picker's closed candidate set — Google
    Places hits returned by `PlacesSearcher`. The picker may only emit
    places whose `external_id` matches an entry here.
    """

    url: str | None
    user_id: str
    supplementary_text: str = ""
    caption: str | None = None
    transcript: str | None = None
    platform: str | None = None
    title: str | None = None
    hashtags: list[str] = field(default_factory=list)
    location_tag: str | None = None
    source: PlaceSource | None = None
    known_places: list[KnownPlace] = field(default_factory=list)
    text_evidence: list[Evidence] = field(default_factory=list)
    is_photo_post: bool = False
    image_urls: list[str] = field(default_factory=list)
    search_matches: list[SearchMatch] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source is None:
            from totoro_ai.core.extraction.url_source import source_from_url

            self.source = source_from_url(self.url)


@dataclass
class ValidatedCandidate:
    """Post-validation extraction candidate.

    The validator stamps `provider` + `external_id` (resolved from
    Google Places) onto the candidate fields and adds `confidence` plus
    the same `evidence` list the candidate carried in. `match_lat` /
    `match_lng` / `match_address` carry the Tier 2 geo data Google
    Places returned so the persistence layer can write it to
    `PlacesCache` after the Tier 1 row is created. All three geo fields
    are optional — `None` when Google returned NONE-quality or the
    validator was bypassed.
    """

    place_name: str
    place_type: PlaceType
    provider: PlaceProvider
    external_id: str
    confidence: float
    evidence: list[Evidence]
    subcategory: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: PlaceAttributes = field(default_factory=PlaceAttributes)
    match_lat: float | None = None
    match_lng: float | None = None
    match_address: str | None = None
