"""Tests for dedup_by_provider_id (post-pick dedup, v2 vocabulary)."""

import pytest

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.dedup import dedup_by_provider_id
from kebi.core.extraction.types import (
    Evidence,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places_v2 import (
    LocationContext,
    PlaceCategory,
    PlaceTag,
    TagType,
)


def _make_validated(
    place_name: str = "Ramen Kaisugi",
    provider_id: str = "google:ChIJrUYs1Xuf4jARDnd40CFUUAE",
    confidence: float = 0.85,
    evidence: list[Evidence] | None = None,
    categories: list[PlaceCategory] | None = None,
    tags: list[PlaceTag] | None = None,
    city: str | None = "Bangkok",
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        provider_id=provider_id,
        categories=categories or [PlaceCategory.restaurant],
        tags=tags or [],
        confidence=confidence,
        evidence=evidence or [Evidence(Producer.LLM_NER, Medium.CAPTION)],
        location=LocationContext(city=city) if city else None,
    )


def _config(
    corroboration_bonus: float = 0.10, max_score: float = 0.97
) -> ConfidenceConfig:
    return ConfidenceConfig(
        corroboration_bonus=corroboration_bonus, max_score=max_score
    )


def test_single_result_unchanged() -> None:
    [vc] = dedup_by_provider_id([_make_validated()], _config())
    assert vc.place_name == "Ramen Kaisugi"


def test_no_dups_passes_through() -> None:
    a = _make_validated(provider_id="google:A", confidence=0.8)
    b = _make_validated(provider_id="google:B", confidence=0.6)
    out = dedup_by_provider_id([a, b], _config())
    assert {v.provider_id for v in out} == {"google:A", "google:B"}


def test_same_provider_id_collapses_max_confidence() -> None:
    a = _make_validated(provider_id="google:X", confidence=0.6)
    b = _make_validated(provider_id="google:X", confidence=0.8)
    [out] = dedup_by_provider_id([a, b], _config())
    # max(0.6, 0.8) = 0.8 — no corroboration bonus applied because
    # both candidates have identical (producer, medium) evidence.
    assert out.confidence == 0.8


def test_evidence_unioned() -> None:
    a = _make_validated(
        provider_id="google:X",
        confidence=0.6,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )
    b = _make_validated(
        provider_id="google:X",
        confidence=0.65,
        evidence=[
            Evidence(Producer.LLM_NER, Medium.CAPTION),
            Evidence(Producer.VIDEO_METADATA, Medium.TITLE),
        ],
    )
    [out] = dedup_by_provider_id([a, b], _config())
    media = {(e.producer, e.medium) for e in out.evidence}
    assert media == {
        (Producer.LLM_NER, Medium.CAPTION),
        (Producer.VIDEO_METADATA, Medium.TITLE),
    }


def test_corroboration_bonus_applied_on_distinct_producer_pairs() -> None:
    a = _make_validated(
        provider_id="google:X",
        confidence=0.7,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )
    b = _make_validated(
        provider_id="google:X",
        confidence=0.7,
        evidence=[Evidence(Producer.GOOGLE_MAPS_LIST, Medium.LIST)],
    )
    [out] = dedup_by_provider_id([a, b], _config(corroboration_bonus=0.1))
    # max(0.7, 0.7) + 0.1 = 0.8
    assert out.confidence == pytest.approx(0.8)


def test_corroboration_bonus_capped() -> None:
    a = _make_validated(
        provider_id="google:X",
        confidence=0.95,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )
    b = _make_validated(
        provider_id="google:X",
        confidence=0.95,
        evidence=[Evidence(Producer.GOOGLE_MAPS_LIST, Medium.LIST)],
    )
    [out] = dedup_by_provider_id(
        [a, b], _config(corroboration_bonus=0.1, max_score=0.97)
    )
    assert out.confidence == 0.97


def test_categories_unioned_on_merge() -> None:
    a = _make_validated(
        provider_id="google:X",
        categories=[PlaceCategory.restaurant],
    )
    b = _make_validated(
        provider_id="google:X",
        categories=[PlaceCategory.bar],
    )
    [out] = dedup_by_provider_id([a, b], _config())
    assert PlaceCategory.restaurant in out.categories
    assert PlaceCategory.bar in out.categories


def test_tags_unioned_on_merge_dedup_by_type_value() -> None:
    a = _make_validated(
        provider_id="google:X",
        tags=[PlaceTag(type=TagType.cuisine, value="Japanese", source="llm")],
    )
    b = _make_validated(
        provider_id="google:X",
        tags=[
            PlaceTag(type=TagType.cuisine, value="Japanese", source="llm"),
            PlaceTag(type=TagType.atmosphere, value="cozy", source="llm"),
        ],
    )
    [out] = dedup_by_provider_id([a, b], _config())
    pairs = {(str(t.type), str(t.value)) for t in out.tags}
    # cuisine/Japanese appears once; atmosphere/cozy added from b.
    assert ("TagType.cuisine", "Japanese") in pairs
    assert ("TagType.atmosphere", "cozy") in pairs
    assert len(out.tags) == 2
