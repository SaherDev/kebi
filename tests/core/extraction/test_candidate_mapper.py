"""Tests for candidate_mapper shared helpers (ADR-080)."""

from __future__ import annotations

from dataclasses import dataclass

from kebi.core.extraction.candidate_mapper import (
    AttributedSearchResult,
    llm_tags_to_place_tags,
    location_hint_from,
    merge_tags,
)
from kebi.core.extraction.types import ExtractionContext, Medium, Producer
from kebi.core.places import PlaceObject, PlaceTag, TagType


@dataclass
class _Tag:
    type: str
    value: str


def test_llm_tags_to_place_tags_maps_known_and_unknown_types() -> None:
    out = llm_tags_to_place_tags(
        [
            _Tag(type="atmosphere", value="upscale"),
            _Tag(type="made_up_axis", value="weird"),
            _Tag(type="", value="skip"),
            _Tag(type="time", value=""),
        ]
    )
    assert [(t.type, t.value, t.source) for t in out] == [
        (TagType.atmosphere, "upscale", "llm"),
        ("made_up_axis", "weird", "llm"),
    ]


def test_merge_tags_dedupes_exact_pair_keeping_per_place() -> None:
    per_place = [
        PlaceTag(type=TagType.price, value="moderate", source="google"),
        PlaceTag(type=TagType.atmosphere, value="cozy", source="llm"),
    ]
    shared = [
        PlaceTag(type=TagType.price, value="moderate", source="llm"),  # exact dup
        PlaceTag(type=TagType.price, value="very_expensive", source="llm"),
        PlaceTag(type=TagType.time, value="dinner", source="llm"),
    ]
    merged = merge_tags(per_place, shared)
    pairs = [(t.type, t.value) for t in merged]

    # Exact (price, moderate) dup dropped; the per-place copy (google) kept.
    assert pairs.count((TagType.price, "moderate")) == 1
    assert merged[0].source == "google"
    # Distinct same-type value coexists (dedupe is by (type, value)).
    assert (TagType.price, "very_expensive") in pairs
    assert (TagType.time, "dinner") in pairs
    # Per-place tags first, in order.
    assert [t.value for t in merged[:2]] == ["moderate", "cozy"]
    assert len(merged) == 4


def test_merge_tags_empty_shared_is_noop() -> None:
    per_place = [PlaceTag(type=TagType.atmosphere, value="cozy", source="llm")]
    assert merge_tags(per_place, []) == per_place


def test_attributed_search_result_search_query_defaults_empty() -> None:
    ar = AttributedSearchResult(
        place=PlaceObject(provider_id="google:x", place_name="X", categories=[]),
        query="raw name",
        query_producer=Producer.VISION_IMAGES,
        query_medium=Medium.IMAGE,
    )
    assert ar.search_query == ""


def test_location_hint_from_uses_location_tag() -> None:
    ctx = ExtractionContext(url=None, user_id="u1")
    assert location_hint_from(ctx) is None
    ctx.location_tag = "  Bangkok  "
    hint = location_hint_from(ctx)
    assert hint is not None and hint.address == "Bangkok"
