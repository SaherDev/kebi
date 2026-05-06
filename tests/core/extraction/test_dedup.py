"""Tests for dedup_validated_by_provider_id (post-pick dedup)."""

import pytest

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.dedup import dedup_validated_by_provider_id
from totoro_ai.core.extraction.types import (
    Evidence,
    Medium,
    Producer,
    ValidatedCandidate,
)
from totoro_ai.core.places import (
    LocationContext,
    PlaceAttributes,
    PlaceProvider,
    PlaceType,
)


def _attrs(
    cuisine: str | None = None,
    price_hint: str | None = None,
    city: str | None = None,
) -> PlaceAttributes:
    return PlaceAttributes(
        cuisine=cuisine,
        price_hint=price_hint,
        location_context=LocationContext(city=city) if city else None,
    )


def _make_validated(
    place_name: str = "Ramen Kaisugi",
    external_id: str = "ChIJrUYs1Xuf4jARDnd40CFUUAE",
    confidence: float = 0.85,
    evidence: list[Evidence] | None = None,
    cuisine: str | None = None,
    city: str | None = "Bangkok",
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.google,
        external_id=external_id,
        confidence=confidence,
        evidence=evidence or [Evidence(Producer.LLM_NER, Medium.CAPTION)],
        attributes=_attrs(cuisine=cuisine, city=city),
    )


def _config(
    corroboration_bonus: float = 0.10, max_score: float = 0.97
) -> ConfidenceConfig:
    return ConfidenceConfig(
        corroboration_bonus=corroboration_bonus, max_score=max_score
    )


def test_single_result_unchanged() -> None:
    result = _make_validated()
    out = dedup_validated_by_provider_id([result], _config())
    assert out == [result]


def test_two_different_external_ids_both_kept() -> None:
    a = _make_validated(external_id="id_a")
    b = _make_validated(external_id="id_b")
    out = dedup_validated_by_provider_id([a, b], _config())
    assert len(out) == 2


def test_same_provider_id_merges_evidence() -> None:
    a = _make_validated(
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)]
    )
    b = _make_validated(
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)]
    )
    out = dedup_validated_by_provider_id([a, b], _config())
    assert len(out) == 1
    producers = {e.producer for e in out[0].evidence}
    assert producers == {Producer.LLM_NER, Producer.VISION_FRAMES}


def test_corroboration_bonus_applied_when_two_distinct_pairs() -> None:
    a = _make_validated(
        confidence=0.76, evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)]
    )
    b = _make_validated(
        confidence=0.64,
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)],
    )
    out = dedup_validated_by_provider_id(
        [a, b], _config(corroboration_bonus=0.10, max_score=0.97)
    )
    # Confidence becomes max(0.76, 0.64) + 0.10 = 0.86.
    assert out[0].confidence == pytest.approx(0.86)


def test_corroboration_bonus_capped_at_max_score() -> None:
    a = _make_validated(
        confidence=0.95, evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)]
    )
    b = _make_validated(
        confidence=0.80,
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)],
    )
    out = dedup_validated_by_provider_id(
        [a, b], _config(corroboration_bonus=0.10, max_score=0.97)
    )
    assert out[0].confidence == pytest.approx(0.97)


def test_no_bonus_when_only_one_distinct_pair() -> None:
    """Same (producer, medium) on both items — no corroboration bonus."""
    same_pair = Evidence(Producer.LLM_NER, Medium.CAPTION)
    a = _make_validated(confidence=0.76, evidence=[same_pair])
    b = _make_validated(confidence=0.64, evidence=[same_pair])
    out = dedup_validated_by_provider_id(
        [a, b], _config(corroboration_bonus=0.10, max_score=0.97)
    )
    assert out[0].confidence == pytest.approx(0.76)


def test_empty_results_returns_empty() -> None:
    assert dedup_validated_by_provider_id([], _config()) == []


def test_dedup_validated_inherits_attributes_from_loser() -> None:
    a = _make_validated(
        confidence=0.80,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
        cuisine=None,
        city=None,
    )
    b = _make_validated(
        confidence=0.65,
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)],
        cuisine="ramen",
        city="Bangkok",
    )
    out = dedup_validated_by_provider_id([a, b], _config())
    assert len(out) == 1
    winner = out[0]
    assert winner.attributes.cuisine == "ramen"
    assert winner.attributes.location_context is not None
    assert winner.attributes.location_context.city == "Bangkok"
