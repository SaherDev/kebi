"""Tests for extraction-cascade types."""

from totoro_ai.core.extraction.types import (
    CandidatePlace,
    Evidence,
    ExtractionContext,
    KnownPlace,
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


def _evidence(
    producer: Producer = Producer.LLM_NER,
    medium: Medium = Medium.CAPTION,
    snippet: str | None = "Loved Fuji Ramen",
) -> Evidence:
    return Evidence(producer=producer, medium=medium, snippet=snippet)


def _candidate(
    name: str = "Fuji Ramen",
    evidence: list[Evidence] | None = None,
) -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        evidence=evidence or [_evidence()],
        subcategory="restaurant",
        attributes=PlaceAttributes(
            cuisine="ramen",
            location_context=LocationContext(city="Bangkok"),
        ),
    )


class TestProducer:
    def test_name_producers_present(self) -> None:
        names = {p.value for p in Producer}
        assert {
            "llm_ner",
            "google_maps_list",
            "vision_frames",
            "vision_images",
        } <= names

    def test_text_producers_present(self) -> None:
        names = {p.value for p in Producer}
        assert {
            "tiktok_oembed",
            "ytdlp_metadata",
            "whisper_audio",
            "subtitle_check",
            "photo_detector",
        } <= names


class TestMedium:
    def test_text_media_present(self) -> None:
        names = {m.value for m in Medium}
        assert {
            "caption",
            "transcript",
            "title",
            "hashtag",
            "location_tag",
            "supplementary_text",
            "emoji_marker",
        } <= names

    def test_visual_media_present(self) -> None:
        names = {m.value for m in Medium}
        assert {"frame", "image", "list"} <= names


class TestEvidence:
    def test_frozen_and_hashable(self) -> None:
        a = _evidence()
        b = _evidence()
        # Same content → equal and hash-equal (frozen dataclass).
        assert a == b
        assert hash(a) == hash(b)
        # Set semantics work for dedup.
        assert len({a, b}) == 1

    def test_different_medium_distinct(self) -> None:
        a = _evidence(medium=Medium.CAPTION)
        b = _evidence(medium=Medium.TRANSCRIPT)
        assert a != b
        assert len({a, b}) == 2

    def test_metadata_tuple(self) -> None:
        e = Evidence(
            producer=Producer.PHOTO_DETECTOR,
            medium=Medium.IMAGE,
            metadata=(("image_count", 5),),
        )
        assert dict(e.metadata)["image_count"] == 5


class TestCandidatePlace:
    def test_holds_extraction_fields(self) -> None:
        c = _candidate()
        assert c.place_name == "Fuji Ramen"
        assert c.place_type == PlaceType.food_and_drink
        assert c.subcategory == "restaurant"
        assert c.attributes.cuisine == "ramen"
        assert len(c.evidence) == 1
        assert c.evidence[0].producer == Producer.LLM_NER

    def test_evidence_can_carry_multiple_items(self) -> None:
        c = CandidatePlace(
            place_name="Fuji Ramen",
            place_type=PlaceType.food_and_drink,
            evidence=[
                Evidence(Producer.LLM_NER, Medium.CAPTION),
                Evidence(Producer.VISION_FRAMES, Medium.FRAME, snippet="Fuji Ramen"),
            ],
        )
        assert {e.producer for e in c.evidence} == {
            Producer.LLM_NER,
            Producer.VISION_FRAMES,
        }


class TestExtractionContext:
    def test_instantiation_url(self) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        assert ctx.url == "https://tiktok.com/v/123"
        assert ctx.user_id == "u1"
        assert ctx.candidates == []
        assert ctx.known_places == []
        assert ctx.text_evidence == []

    def test_independent_per_instance(self) -> None:
        ctx1 = ExtractionContext(url=None, user_id="u1")
        ctx2 = ExtractionContext(url=None, user_id="u2")
        ctx1.candidates.append(_candidate(name="A"))
        ctx1.text_evidence.append(_evidence())
        ctx1.known_places.append(
            KnownPlace(
                name="X", producer=Producer.GOOGLE_MAPS_LIST, medium=Medium.LIST
            )
        )
        assert ctx2.candidates == []
        assert ctx2.text_evidence == []
        assert ctx2.known_places == []


class TestKnownPlace:
    def test_carries_producer_medium_snippet(self) -> None:
        k = KnownPlace(
            name="Joe's Pizza",
            producer=Producer.GOOGLE_MAPS_LIST,
            medium=Medium.LIST,
            snippet="Joe's Pizza",
        )
        assert k.producer == Producer.GOOGLE_MAPS_LIST
        assert k.medium == Medium.LIST
        assert k.snippet == "Joe's Pizza"


class TestValidatedCandidate:
    def test_instantiation(self) -> None:
        vc = ValidatedCandidate(
            place_name="Fuji Ramen",
            place_type=PlaceType.food_and_drink,
            provider=PlaceProvider.google,
            external_id="ChIJ123",
            confidence=0.95,
            evidence=[_evidence()],
            subcategory="restaurant",
            attributes=PlaceAttributes(cuisine="ramen"),
        )
        assert vc.confidence == 0.95
        assert vc.provider == PlaceProvider.google
        assert vc.external_id == "ChIJ123"
        assert vc.attributes.cuisine == "ramen"
        assert len(vc.evidence) == 1
