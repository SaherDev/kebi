"""Shared types for the extraction cascade.

Every candidate place carries an `evidence` list — one entry per
producer/medium pair that contributed to it. The list is the audit
trail: which enricher saw what, in which slice of pipeline state, with
the actual content snippet when available. `Producer` identifies *who*
contributed; `Medium` identifies *where* in pipeline state the evidence
lived. Together they form `Evidence(producer, medium, snippet, metadata)`.

Pipeline-context fields (`user_id`, `source_ref`, `source`) and
persistence concerns are NOT on these types — they live on
`ExtractionContext` and are stamped onto a v2 `PlaceCore` at the
persistence boundary in `ExtractionService.run` (per ADR-070, ADR-071).

Spec 030 Phase 3: legacy place re-exports were removed. Place identity
(`provider_id`), the place vocabulary (`PlaceCategory`, `PlaceTag`), and
the source enum all come from `kebi.core.places`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from kebi.core.places import (
    LocationContext,
    PlaceCategory,
    PlaceSource,
    PlaceTag,
)

__all__ = [
    "Producer",
    "Medium",
    "EvidenceField",
    "Evidence",
    "KnownPlace",
    "ExtractionContext",
    "ValidatedCandidate",
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

    Carries the `producer` + `medium` + `snippet` so the picker can
    stamp matching candidates with full Evidence — without this,
    every candidate built from `known_places` would look like it came
    only from `LLM_NER`.
    """

    name: str
    producer: Producer
    medium: Medium
    snippet: str | None = None


@dataclass
class ExtractionContext:
    """Shared mutable state threaded through all enrichers and the picker.

    `source` is auto-derived from `url` in `__post_init__` so every
    consumer (enrichers, persistence, the service) reads the same
    canonical v2 `PlaceSource` without re-parsing the URL. Callers
    may pass `source` explicitly to override (e.g. tests).

    `known_places` is a list of `KnownPlace` entries — confirmed venue
    names from name producers (Google Maps shared list, vision frames,
    vision images) plus names the pre-search resolver discovers in the
    post's free text (`LLM_NER`). Each entry carries the
    `producer + medium + snippet` so the picker can stamp matching
    picks with full provenance.

    `text_evidence` is a list of `Evidence` entries appended by text
    producers when they actually wrote pipeline state (caption,
    transcript, title, etc.).
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

    def __post_init__(self) -> None:
        if self.source is None:
            from kebi.core.extraction.url_source import source_from_url

            self.source = source_from_url(self.url)


@dataclass
class ValidatedCandidate:
    """Post-validation extraction candidate (v2 vocabulary).

    The picker emits this shape (via `reconcile_picks`) for every
    chosen `PlaceObject` from `PlacesSearchService`. The persistence
    boundary in `ExtractionService.run` converts each into a v2
    `PlaceCore` via `candidate_to_core` before calling
    `PlaceUpsertService.upsert_and_embed`.

    `confidence` is computed from the evidence trail but is no longer
    a save gate (ADR-071 supersedes ADR-057 — all picker outputs save
    as `user_places` rows with `approved=False`). It's preserved on
    the candidate so the response envelope can still surface it for UI.
    """

    place_name: str
    provider_id: str  # namespaced, e.g. "google:ChIJ..."
    categories: list[PlaceCategory]
    tags: list[PlaceTag]
    confidence: float
    evidence: list[Evidence]
    # Picker-chosen display emoji (already normalized); survives the
    # reconcile pass like categories/tags and lands on PlaceCore.icon.
    icon: str | None = None
    subcategory: str | None = None
    location: LocationContext | None = None
    # The name this place was shown as in the source post (the raw
    # producer label, e.g. a TikTok card title "Mirror Temple"), set
    # only when it differs from the canonical `place_name`. Threaded to
    # the per-user `user_places.source_label` (ungated) and, for
    # high-confidence picks, to the shared `place_name_aliases`.
    source_label: str | None = None
