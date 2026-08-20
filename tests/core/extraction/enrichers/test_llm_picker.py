"""Tests for LLMPlacePicker — v2 vocabulary (spec 030 Phase 3)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.config import ConfidenceConfig
from kebi.core.extraction.candidate_mapper import AttributedSearchResult
from kebi.core.extraction.enrichers.llm_picker import (
    LLMPlacePicker,
    _LLMTag,
    _PickedPlace,
    _PickerResponse,
)
from kebi.core.extraction.types import (
    EvidenceField,
    ExtractionContext,
    Medium,
    Producer,
)
from kebi.core.places import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceTag,
    TagType,
)
from kebi.providers.llm import InstructorExtraction


def _ctx(caption: str = "") -> ExtractionContext:
    ctx = ExtractionContext(url=None, user_id="u1")
    ctx.caption = caption or None
    return ctx


def _attributed(
    provider_id: str = "google:id1",
    place_name: str = "Joe's Pizza",
    categories: list[PlaceCategory] | None = None,
) -> AttributedSearchResult:
    return AttributedSearchResult(
        place=PlaceObject(
            provider_id=provider_id,
            place_name=place_name,
            categories=categories or [PlaceCategory.restaurant],
            location=LocationContext(city="Bangkok"),
        ),
        query=place_name,
        query_producer=Producer.GOOGLE_MAPS_LIST,
        query_medium=Medium.LIST,
    )


def _picker_with_response(response: _PickerResponse) -> LLMPlacePicker:
    instructor = MagicMock()
    instructor.extract = AsyncMock(return_value=InstructorExtraction(data=response))
    return LLMPlacePicker(
        instructor_client=instructor,
        confidence_config=ConfidenceConfig(),
    )


@pytest.mark.asyncio
async def test_empty_search_set_short_circuits() -> None:
    picker = _picker_with_response(_PickerResponse(picks=[]))
    out = await picker.pick(_ctx(), {})
    assert out == []
    picker._instructor_client.extract.assert_not_called()


@pytest.mark.asyncio
async def test_pick_with_valid_provider_id_returns_candidate() -> None:
    attributed = _attributed(provider_id="google:joe")
    search_set = {"google:joe": attributed}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    categories=[PlaceCategory.restaurant],
                    tags=[],
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(caption="pizza in Bangkok"), search_set)
    assert len(out) == 1
    assert out[0].provider_id == "google:joe"
    assert out[0].place_name == "Joe's Pizza"  # echoed from search result
    assert out[0].categories == [PlaceCategory.restaurant]
    assert out[0].confidence > 0.0


@pytest.mark.asyncio
async def test_pick_missing_from_search_set_is_dropped() -> None:
    attributed = _attributed(provider_id="google:joe")
    search_set = {"google:joe": attributed}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:not-in-set",
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(), search_set)
    assert out == []


@pytest.mark.asyncio
async def test_rejected_pick_excluded() -> None:
    search_set = {"google:joe": _attributed(provider_id="google:joe")}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    rejected=True,
                    rejection_reason="wrong city",
                )
            ]
        )
    )
    out = await picker.pick(_ctx(), search_set)
    assert out == []


@pytest.mark.asyncio
async def test_tags_converted_to_place_tag_with_source_llm() -> None:
    search_set = {"google:joe": _attributed(provider_id="google:joe")}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    tags=[
                        _LLMTag(type="cuisine", value="Italian"),
                        _LLMTag(type="atmosphere", value="cozy"),
                    ],
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(caption="pizza"), search_set)
    assert len(out) == 1
    tag_pairs = {(str(t.type), str(t.value)) for t in out[0].tags}
    # type may resolve to TagType enum or stay as str — both forms accepted.
    assert any(v == "Italian" for _, v in tag_pairs)
    assert any(v == "cozy" for _, v in tag_pairs)
    assert all(t.source == "llm" for t in out[0].tags)


@pytest.mark.asyncio
async def test_shared_tags_merged_into_pick_per_place_wins() -> None:
    search_set = {"google:joe": _attributed(provider_id="google:joe")}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    tags=[_LLMTag(type="price", value="moderate")],
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    shared = [
        PlaceTag(type=TagType.price, value="moderate", source="llm"),  # exact dup
        PlaceTag(type=TagType.atmosphere, value="upscale", source="llm"),
    ]
    out = await picker.pick(_ctx(caption="x"), search_set, shared_tags=shared)
    assert len(out) == 1
    tags = out[0].tags
    values = [t.value for t in tags]
    # exact (price, moderate) dup not duplicated; shared atmosphere added.
    assert values.count("moderate") == 1
    assert "upscale" in values
    assert tags[0].value == "moderate"  # per-place tag stays first


@pytest.mark.asyncio
async def test_shared_tags_none_preserves_behavior() -> None:
    search_set = {"google:joe": _attributed(provider_id="google:joe")}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    tags=[_LLMTag(type="cuisine", value="Italian")],
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(caption="x"), search_set)
    assert len(out) == 1
    assert {t.value for t in out[0].tags} == {"Italian"}


@pytest.mark.asyncio
async def test_instructor_exception_returns_empty_list() -> None:
    instructor = MagicMock()
    instructor.extract = AsyncMock(side_effect=RuntimeError("network down"))
    picker = LLMPlacePicker(
        instructor_client=instructor, confidence_config=ConfidenceConfig()
    )
    out = await picker.pick(
        _ctx(),
        {"google:joe": _attributed(provider_id="google:joe")},
    )
    assert out == []


@pytest.mark.asyncio
async def test_subcategory_null_string_normalized_to_none() -> None:
    search_set = {"google:joe": _attributed(provider_id="google:joe")}
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    subcategory="null",
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(), search_set)
    assert out[0].subcategory is None


@pytest.mark.asyncio
async def test_icon_threads_through_to_validated_candidate() -> None:
    attributed = _attributed(provider_id="google:joe")
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    icon="🍕",
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(caption="pizza"), {"google:joe": attributed})
    assert out[0].icon == "🍕"


@pytest.mark.asyncio
async def test_junk_icon_normalized_to_none() -> None:
    attributed = _attributed(provider_id="google:joe")
    picker = _picker_with_response(
        _PickerResponse(
            picks=[
                _PickedPlace(
                    provider_id="google:joe",
                    icon="pizza place",
                    evidence_fields=[EvidenceField.CAPTION],
                )
            ]
        )
    )
    out = await picker.pick(_ctx(caption="pizza"), {"google:joe": attributed})
    assert out[0].icon is None
