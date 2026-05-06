"""Tests for extraction-cascade types."""

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


def _candidate(
    name: str = "Fuji Ramen",
    source: ExtractionLevel = ExtractionLevel.LLM_NER,
) -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        source=source,
        subcategory="restaurant",
        attributes=PlaceAttributes(
            cuisine="ramen",
            location_context=LocationContext(city="Bangkok"),
        ),
    )


class TestExtractionLevel:
    def test_enum_values(self) -> None:
        assert ExtractionLevel.EMOJI_REGEX.value == "emoji_regex"
        assert ExtractionLevel.LLM_NER.value == "llm_ner"
        assert ExtractionLevel.SUBTITLE_CHECK.value == "subtitle_check"
        assert ExtractionLevel.WHISPER_AUDIO.value == "whisper_audio"
        assert ExtractionLevel.VISION_FRAMES.value == "vision_frames"
        assert ExtractionLevel.GOOGLE_MAPS_LIST.value == "google_maps_list"

    def test_enum_has_six_members(self) -> None:
        assert len(ExtractionLevel) == 6


class TestCandidatePlace:
    def test_holds_extraction_fields(self) -> None:
        c = _candidate(source=ExtractionLevel.EMOJI_REGEX)
        assert c.place_name == "Fuji Ramen"
        assert c.place_type == PlaceType.food_and_drink
        assert c.subcategory == "restaurant"
        assert c.attributes.cuisine == "ramen"
        assert c.source == ExtractionLevel.EMOJI_REGEX
        assert c.corroborated is False
        assert c.signals == []

    def test_corroborated_and_signals_can_be_set(self) -> None:
        c = CandidatePlace(
            place_name="Fuji Ramen",
            place_type=PlaceType.food_and_drink,
            source=ExtractionLevel.LLM_NER,
            corroborated=True,
            signals=["caption"],
        )
        assert c.corroborated is True
        assert c.signals == ["caption"]


class TestExtractionContext:
    def test_instantiation_url(self) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        assert ctx.url == "https://tiktok.com/v/123"
        assert ctx.user_id == "u1"
        assert ctx.supplementary_text == ""
        assert ctx.caption is None
        assert ctx.transcript is None
        assert ctx.candidates == []

    def test_candidates_are_independent_instances(self) -> None:
        ctx1 = ExtractionContext(url=None, user_id="u1")
        ctx2 = ExtractionContext(url=None, user_id="u2")
        ctx1.candidates.append(_candidate(name="A"))
        assert ctx2.candidates == []


class TestValidatedCandidate:
    def test_instantiation(self) -> None:
        vc = ValidatedCandidate(
            place_name="Fuji Ramen",
            place_type=PlaceType.food_and_drink,
            provider=PlaceProvider.google,
            external_id="ChIJ123",
            confidence=0.95,
            resolved_by=ExtractionLevel.EMOJI_REGEX,
            subcategory="restaurant",
            attributes=PlaceAttributes(
                cuisine="ramen",
                location_context=LocationContext(city="Bangkok"),
            ),
        )
        assert vc.confidence == 0.95
        assert vc.resolved_by == ExtractionLevel.EMOJI_REGEX
        assert vc.provider == PlaceProvider.google
        assert vc.external_id == "ChIJ123"
        assert vc.attributes.cuisine == "ramen"
        # City lives on attributes.location_context, not on the wrapper.
        assert vc.attributes.location_context is not None
        assert vc.attributes.location_context.city == "Bangkok"
