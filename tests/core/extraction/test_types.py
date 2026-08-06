"""Tests for extraction-cascade types (v2 vocabulary)."""

from kebi.core.extraction.types import (
    Evidence,
    EvidenceField,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places import LocationContext, PlaceCategory, PlaceTag, TagType


def _evidence(
    producer: Producer = Producer.LLM_NER,
    medium: Medium = Medium.CAPTION,
    snippet: str | None = "Loved Fuji Ramen",
) -> Evidence:
    return Evidence(producer=producer, medium=medium, snippet=snippet)


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
            "tiktok_caption",
            "video_metadata",
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


class TestEvidenceField:
    def test_text_fields_present(self) -> None:
        names = {f.value for f in EvidenceField}
        assert {
            "caption",
            "transcript",
            "title",
            "hashtag",
            "location_tag",
            "supplementary_text",
            "known_places",
        } <= names


class TestEvidence:
    def test_frozen_and_hashable(self) -> None:
        a = _evidence()
        b = _evidence()
        assert a == b
        assert hash(a) == hash(b)
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


class TestExtractionContext:
    def test_instantiation_url(self) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        assert ctx.url == "https://tiktok.com/v/123"
        assert ctx.user_id == "u1"
        assert ctx.known_places == []
        assert ctx.text_evidence == []

    def test_source_derived_from_url(self) -> None:
        from kebi.core.places import PlaceSource

        ctx = ExtractionContext(url="https://www.instagram.com/p/x/", user_id="u1")
        assert ctx.source == PlaceSource.instagram

    def test_independent_per_instance(self) -> None:
        ctx1 = ExtractionContext(url=None, user_id="u1")
        ctx2 = ExtractionContext(url=None, user_id="u2")
        ctx1.text_evidence.append(_evidence())
        ctx1.known_places.append(
            KnownPlace(name="X", producer=Producer.GOOGLE_MAPS_LIST, medium=Medium.LIST)
        )
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
    def test_instantiation_v2_vocab(self) -> None:
        vc = ValidatedCandidate(
            place_name="Fuji Ramen",
            provider_id="google:ChIJ123",
            categories=[PlaceCategory.restaurant],
            tags=[PlaceTag(type=TagType.cuisine, value="Japanese", source="llm")],
            confidence=0.95,
            evidence=[_evidence()],
            subcategory="ramen",
            location=LocationContext(city="Bangkok"),
        )
        assert vc.confidence == 0.95
        assert vc.provider_id == "google:ChIJ123"
        assert vc.categories == [PlaceCategory.restaurant]
        assert len(vc.tags) == 1
        assert vc.tags[0].value == "Japanese"
        assert vc.location is not None
        assert vc.location.city == "Bangkok"
        assert len(vc.evidence) == 1
