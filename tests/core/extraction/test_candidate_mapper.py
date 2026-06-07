"""Tests for candidate_mapper shared helpers (ADR-080)."""

from __future__ import annotations

from dataclasses import dataclass

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.candidate_mapper import (
    AttributedSearchResult,
    candidate_to_core,
    llm_tags_to_place_tags,
    location_hint_from,
    merge_tags,
    reconcile_picks,
)
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places import (
    PlaceCategory,
    PlaceNameAlias,
    PlaceObject,
    PlaceTag,
    TagType,
)


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


# ---------------------------------------------------------------------------
# ADR-081: candidate_to_core aliases + reconcile_picks source_label
# ---------------------------------------------------------------------------


def _vc(provider_id: str = "google:x") -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name="ignored — reconcile sources it from the search hit",
        provider_id=provider_id,
        categories=[PlaceCategory.restaurant],
        tags=[],
        confidence=0.0,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )


def test_candidate_to_core_aliases_optional() -> None:
    c = _vc()
    assert candidate_to_core(c).place_name_aliases == []
    alias = [PlaceNameAlias(value="Mirror Temple", source="tiktok")]
    assert candidate_to_core(c, aliases=alias).place_name_aliases == alias


def _search_set(
    raw_label: str, canonical: str, display_label: str = ""
) -> dict[str, AttributedSearchResult]:
    return {
        "google:x": AttributedSearchResult(
            place=PlaceObject(
                provider_id="google:x", place_name=canonical, categories=[]
            ),
            query=raw_label,
            query_producer=Producer.VISION_FRAMES,
            query_medium=Medium.FRAME,
            display_label=display_label,
        )
    }


def test_reconcile_uses_resolver_display_label() -> None:
    """The resolver already stripped the list number; reconcile takes
    the clean display_label verbatim (search hit is the real name)."""
    out = reconcile_picks(
        [_vc()],
        _search_set(
            "1. Mirror Temple",  # raw producer name (numbered)
            "Wat Phuttha Prommayan",  # canonical from search
            display_label="Mirror Temple",  # resolver-cleaned
        ),
        ConfidenceConfig(),
        ExtractionContext(url=None, user_id="u1"),
    )
    assert out[0].place_name == "Wat Phuttha Prommayan"
    assert out[0].source_label == "Mirror Temple"


def test_reconcile_source_label_none_when_display_equals_canonical() -> None:
    out = reconcile_picks(
        [_vc()],
        _search_set("3. Wat Paknam", "Wat Paknam", display_label="Wat Paknam"),
        ConfidenceConfig(),
        ExtractionContext(url=None, user_id="u1"),
    )
    assert out[0].source_label is None


def test_reconcile_falls_back_to_query_when_no_display_label() -> None:
    """Degraded resolver path: no display_label → use the raw query."""
    out = reconcile_picks(
        [_vc()],
        _search_set("Joe's Pizza", "Joe's Pizza Tokyo"),
        ConfidenceConfig(),
        ExtractionContext(url=None, user_id="u1"),
    )
    assert out[0].source_label == "Joe's Pizza"


def test_reconcile_sets_source_label_when_raw_differs_from_canonical() -> None:
    ctx = ExtractionContext(url=None, user_id="u1")
    out = reconcile_picks(
        [_vc()],
        _search_set("Mirror Temple", "Wat Phuttha Prommayan"),
        ConfidenceConfig(),
        ctx,
    )
    assert len(out) == 1
    assert out[0].place_name == "Wat Phuttha Prommayan"
    assert out[0].source_label == "Mirror Temple"


def test_reconcile_source_label_none_when_normalized_equal() -> None:
    ctx = ExtractionContext(url=None, user_id="u1")
    # Differs only by case/punctuation/whitespace → not a distinct label.
    out = reconcile_picks(
        [_vc()],
        _search_set("  joe's  PIZZA ", "Joe's Pizza"),
        ConfidenceConfig(),
        ctx,
    )
    assert len(out) == 1
    assert out[0].source_label is None


# ---------------------------------------------------------------------------
# reconcile_picks category fallback — picker-empty must not blank a place that
# Google classified (otherwise the product app shows a generic pin avatar).
# ---------------------------------------------------------------------------


def _search_set_with_categories(
    canonical: str, categories: list[PlaceCategory]
) -> dict[str, AttributedSearchResult]:
    return {
        "google:x": AttributedSearchResult(
            place=PlaceObject(
                provider_id="google:x", place_name=canonical, categories=categories
            ),
            query=canonical,
            query_producer=Producer.VISION_FRAMES,
            query_medium=Medium.FRAME,
        )
    }


def _vc_categories(categories: list[PlaceCategory]) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name="ignored — reconcile sources it from the search hit",
        provider_id="google:x",
        categories=categories,
        tags=[],
        confidence=0.0,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )


def test_reconcile_falls_back_to_google_categories_when_picker_empty() -> None:
    """The picker emits no categories for an "obvious" place (e.g. a temple);
    reconcile keeps the v2 PlaceObject's Google-mapped categories rather than
    blanking them."""
    out = reconcile_picks(
        [_vc_categories([])],
        _search_set_with_categories("Wat Phra Yai", [PlaceCategory.temple]),
        ConfidenceConfig(),
        ExtractionContext(url=None, user_id="u1"),
    )
    assert len(out) == 1
    assert out[0].categories == [PlaceCategory.temple]


def test_reconcile_keeps_picker_categories_when_present() -> None:
    """A non-empty picker classification still wins over Google's."""
    out = reconcile_picks(
        [_vc_categories([PlaceCategory.cafe])],
        _search_set_with_categories("X", [PlaceCategory.landmark]),
        ConfidenceConfig(),
        ExtractionContext(url=None, user_id="u1"),
    )
    assert out[0].categories == [PlaceCategory.cafe]
