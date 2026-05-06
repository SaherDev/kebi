"""Tests for dedup_candidates and dedup_validated_by_provider_id."""

import pytest

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.dedup import (
    dedup_candidates,
    dedup_validated_by_provider_id,
)
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ExtractionContext,
    ExtractionLevel,
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


def _ctx(*candidates: CandidatePlace) -> ExtractionContext:
    ctx = ExtractionContext(url=None, user_id="u1")
    ctx.candidates = list(candidates)
    return ctx


def _candidate(
    name: str = "Ramen House",
    source: ExtractionLevel = ExtractionLevel.EMOJI_REGEX,
    corroborated: bool = False,
    cuisine: str | None = None,
    city: str | None = None,
) -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        source=source,
        attributes=_attrs(cuisine=cuisine, city=city),
        corroborated=corroborated,
    )


# ---------------------------------------------------------------------------
# dedup_candidates
# ---------------------------------------------------------------------------


def test_single_candidate_unchanged() -> None:
    c = _candidate("Ramen House")
    ctx = _ctx(c)
    dedup_candidates(ctx)
    assert len(ctx.candidates) == 1
    assert ctx.candidates[0].place_name == "Ramen House"
    assert ctx.candidates[0].corroborated is False


def test_two_different_names_both_kept() -> None:
    ctx = _ctx(_candidate("Ramen House"), _candidate("Sushi Bar"))
    dedup_candidates(ctx)
    assert len(ctx.candidates) == 2


def test_same_name_different_levels_lower_index_wins() -> None:
    emoji = _candidate("Ramen House", source=ExtractionLevel.EMOJI_REGEX)
    ner = _candidate("Ramen House", source=ExtractionLevel.LLM_NER)
    ctx = _ctx(emoji, ner)
    dedup_candidates(ctx)

    assert len(ctx.candidates) == 1
    winner = ctx.candidates[0]
    assert winner.source == ExtractionLevel.EMOJI_REGEX
    assert winner.corroborated is True


def test_three_candidates_two_same_one_different() -> None:
    regex = _candidate("Ramen House", source=ExtractionLevel.EMOJI_REGEX)
    ner = _candidate("Ramen House", source=ExtractionLevel.LLM_NER)
    other = _candidate("Sushi Bar", source=ExtractionLevel.LLM_NER)
    ctx = _ctx(regex, ner, other)
    dedup_candidates(ctx)

    assert len(ctx.candidates) == 2
    names = [c.place_name for c in ctx.candidates]
    assert "Ramen House" in names
    assert "Sushi Bar" in names
    ramen = next(c for c in ctx.candidates if c.place_name == "Ramen House")
    assert ramen.corroborated is True


def test_empty_candidates_noop() -> None:
    ctx = ExtractionContext(url=None, user_id="u1")
    ctx.candidates = []
    dedup_candidates(ctx)
    assert ctx.candidates == []


def test_same_name_different_case_merged() -> None:
    emoji = _candidate("RAMEN KAISUGI", source=ExtractionLevel.EMOJI_REGEX)
    ner = _candidate("ramen kaisugi", source=ExtractionLevel.LLM_NER)
    ctx = _ctx(emoji, ner)
    dedup_candidates(ctx)

    assert len(ctx.candidates) == 1
    assert ctx.candidates[0].source == ExtractionLevel.EMOJI_REGEX
    assert ctx.candidates[0].corroborated is True


def test_same_name_with_punctuation_merged() -> None:
    emoji = _candidate("RAMEN KAISUGI!", source=ExtractionLevel.EMOJI_REGEX)
    ner = _candidate("RAMEN KAISUGI", source=ExtractionLevel.LLM_NER)
    ctx = _ctx(emoji, ner)
    dedup_candidates(ctx)

    assert len(ctx.candidates) == 1
    assert ctx.candidates[0].corroborated is True


def test_dedup_candidates_inherits_attributes_from_loser() -> None:
    """Winner with no cuisine inherits from a loser that had one."""
    winner = _candidate("Ramen House", source=ExtractionLevel.EMOJI_REGEX, cuisine=None)
    loser = _candidate("Ramen House", source=ExtractionLevel.LLM_NER, cuisine="ramen")
    ctx = _ctx(winner, loser)
    dedup_candidates(ctx)

    assert len(ctx.candidates) == 1
    winner_out = ctx.candidates[0]
    assert winner_out.source == ExtractionLevel.EMOJI_REGEX
    assert winner_out.attributes.cuisine == "ramen"


# ---------------------------------------------------------------------------
# dedup_validated_by_provider_id
# ---------------------------------------------------------------------------


def _make_validated(
    place_name: str = "Ramen Kaisugi",
    resolved_by: ExtractionLevel = ExtractionLevel.EMOJI_REGEX,
    external_id: str = "ChIJrUYs1Xuf4jARDnd40CFUUAE",
    confidence: float = 0.85,
    corroborated: bool = False,
    cuisine: str | None = None,
    city: str | None = "Bangkok",
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.google,
        external_id=external_id,
        confidence=confidence,
        resolved_by=resolved_by,
        attributes=_attrs(cuisine=cuisine, city=city),
        corroborated=corroborated,
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
    assert out[0].corroborated is False


def test_two_different_external_ids_both_kept() -> None:
    a = _make_validated(external_id="id_a")
    b = _make_validated(external_id="id_b")
    out = dedup_validated_by_provider_id([a, b], _config())
    assert len(out) == 2


def test_same_provider_id_emoji_wins_over_ner() -> None:
    emoji = _make_validated(resolved_by=ExtractionLevel.EMOJI_REGEX, confidence=0.76)
    ner = _make_validated(resolved_by=ExtractionLevel.LLM_NER, confidence=0.64)
    out = dedup_validated_by_provider_id([emoji, ner], _config())

    assert len(out) == 1
    assert out[0].resolved_by == ExtractionLevel.EMOJI_REGEX


def test_corroboration_bonus_applied_to_winner() -> None:
    emoji = _make_validated(resolved_by=ExtractionLevel.EMOJI_REGEX, confidence=0.76)
    ner = _make_validated(resolved_by=ExtractionLevel.LLM_NER, confidence=0.64)
    out = dedup_validated_by_provider_id(
        [emoji, ner], _config(corroboration_bonus=0.10, max_score=0.97)
    )

    assert out[0].confidence == pytest.approx(0.86)
    assert out[0].corroborated is True


def test_corroboration_bonus_capped_at_max_score() -> None:
    emoji = _make_validated(resolved_by=ExtractionLevel.EMOJI_REGEX, confidence=0.95)
    ner = _make_validated(resolved_by=ExtractionLevel.LLM_NER, confidence=0.80)
    out = dedup_validated_by_provider_id(
        [emoji, ner], _config(corroboration_bonus=0.10, max_score=0.97)
    )

    assert out[0].confidence == pytest.approx(0.97)


def test_empty_results_returns_empty() -> None:
    assert dedup_validated_by_provider_id([], _config()) == []


def test_dedup_validated_inherits_attributes_from_loser() -> None:
    """Winner with no cuisine/city inherits from a loser — deep attribute merge."""
    emoji_winner = _make_validated(
        resolved_by=ExtractionLevel.EMOJI_REGEX,
        external_id="same_id",
        confidence=0.80,
        cuisine=None,
        city=None,
    )
    ner_loser = _make_validated(
        resolved_by=ExtractionLevel.LLM_NER,
        external_id="same_id",
        confidence=0.65,
        cuisine="ramen",
        city="Bangkok",
    )
    out = dedup_validated_by_provider_id([emoji_winner, ner_loser], _config())

    assert len(out) == 1
    winner = out[0]
    assert winner.resolved_by == ExtractionLevel.EMOJI_REGEX
    assert winner.attributes.cuisine == "ramen"
    assert winner.attributes.location_context is not None
    assert winner.attributes.location_context.city == "Bangkok"
