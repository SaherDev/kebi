"""Tests for LLMPlacePicker — search-first LLM step."""

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.enrichers.llm_picker import (
    LLMPlacePicker,
    _PickedPlace,
    _PickerResponse,
)
from totoro_ai.core.extraction.types import (
    EvidenceField,
    ExtractionContext,
    Medium,
    Producer,
    SearchMatch,
)
from totoro_ai.core.places import (
    PlaceAttributes,
    PlaceProvider,
    PlacesMatchQuality,
    PlaceType,
)


def _ctx(matches: list[SearchMatch], caption: str = "") -> ExtractionContext:
    ctx = ExtractionContext(url=None, user_id="u1")
    ctx.caption = caption or None
    ctx.search_matches.extend(matches)
    return ctx


def _match(
    external_id: str = "id_1",
    validated_name: str = "Joe's Pizza",
) -> SearchMatch:
    return SearchMatch(
        query=validated_name,
        query_producer=Producer.GOOGLE_MAPS_LIST,
        query_medium=Medium.LIST,
        validated_name=validated_name,
        provider=PlaceProvider.google,
        external_id=external_id,
        match_quality=PlacesMatchQuality.EXACT,
        lat=13.7,
        lng=100.5,
        address=f"Bangkok, {validated_name}",
        place_types=("restaurant", "food"),
    )


def _picker_with_response(response: _PickerResponse) -> LLMPlacePicker:
    instructor = MagicMock()
    instructor.extract = AsyncMock(return_value=response)
    return LLMPlacePicker(
        instructor_client=instructor,
        confidence_config=ConfidenceConfig(),
    )


async def test_empty_search_matches_returns_empty_no_llm_call() -> None:
    instructor = MagicMock()
    instructor.extract = AsyncMock()
    picker = LLMPlacePicker(
        instructor_client=instructor,
        confidence_config=ConfidenceConfig(),
    )
    ctx = _ctx(matches=[])

    out = await picker.pick(ctx)

    assert out == []
    instructor.extract.assert_not_called()


async def test_pick_emits_validated_candidate_from_search_match() -> None:
    response = _PickerResponse(
        picks=[
            _PickedPlace(
                external_id="id_1",
                place_type=PlaceType.food_and_drink,
                subcategory="restaurant",
                attributes=PlaceAttributes(cuisine="italian", atmosphere=["lively"]),
                evidence_fields=[EvidenceField.CAPTION],
            )
        ]
    )
    picker = _picker_with_response(response)
    ctx = _ctx(
        matches=[_match(external_id="id_1", validated_name="Joe's Pizza")],
        caption="Loved Joe's Pizza last night",
    )

    out = await picker.pick(ctx)

    assert len(out) == 1
    pick = out[0]
    # Name comes from SearchMatch, not from the LLM.
    assert pick.place_name == "Joe's Pizza"
    assert pick.external_id == "id_1"
    assert pick.provider == PlaceProvider.google
    assert pick.subcategory == "restaurant"
    assert pick.attributes.cuisine == "italian"
    assert pick.attributes.atmosphere == ["lively"]


async def test_rejected_picks_are_dropped() -> None:
    response = _PickerResponse(
        picks=[
            _PickedPlace(
                external_id="id_1",
                place_type=PlaceType.food_and_drink,
                rejected=True,
                rejection_reason="caption is in Bangkok, match is in Singapore",
            ),
            _PickedPlace(
                external_id="id_2",
                place_type=PlaceType.food_and_drink,
                evidence_fields=[EvidenceField.CAPTION],
            ),
        ]
    )
    picker = _picker_with_response(response)
    ctx = _ctx(
        matches=[
            _match(external_id="id_1"),
            _match(external_id="id_2", validated_name="Other Place"),
        ]
    )

    out = await picker.pick(ctx)

    assert len(out) == 1
    assert out[0].external_id == "id_2"


async def test_picks_with_unknown_external_id_are_dropped_by_reconcile() -> None:
    """Even if the LLM emits an external_id outside the search set,
    the pipeline drops it."""
    response = _PickerResponse(
        picks=[
            _PickedPlace(
                external_id="hallucinated_id",
                place_type=PlaceType.food_and_drink,
                evidence_fields=[EvidenceField.CAPTION],
            )
        ]
    )
    picker = _picker_with_response(response)
    ctx = _ctx(matches=[_match(external_id="real_id")])

    out = await picker.pick(ctx)

    assert out == []


async def test_evidence_fields_become_llm_ner_evidence_records() -> None:
    response = _PickerResponse(
        picks=[
            _PickedPlace(
                external_id="id_1",
                place_type=PlaceType.food_and_drink,
                evidence_fields=[
                    EvidenceField.CAPTION,
                    EvidenceField.HASHTAG,
                ],
            )
        ]
    )
    picker = _picker_with_response(response)
    ctx = _ctx(matches=[_match(external_id="id_1")])

    out = await picker.pick(ctx)

    media = {ev.medium for ev in out[0].evidence if ev.producer == Producer.LLM_NER}
    assert Medium.CAPTION in media
    assert Medium.HASHTAG in media


async def test_picker_failure_returns_empty() -> None:
    """LLM call failures are swallowed — returns []."""
    instructor = MagicMock()
    instructor.extract = AsyncMock(side_effect=Exception("LLM down"))
    picker = LLMPlacePicker(
        instructor_client=instructor,
        confidence_config=ConfidenceConfig(),
    )
    ctx = _ctx(matches=[_match()])

    out = await picker.pick(ctx)

    assert out == []


async def test_subcategory_null_string_normalized_to_none() -> None:
    """LLMs occasionally emit "null" / "" / "none" instead of JSON null."""
    response = _PickerResponse(
        picks=[
            _PickedPlace(
                external_id="id_1",
                place_type=PlaceType.food_and_drink,
                subcategory="null",
                evidence_fields=[EvidenceField.CAPTION],
            )
        ]
    )
    picker = _picker_with_response(response)
    ctx = _ctx(matches=[_match(external_id="id_1")])

    out = await picker.pick(ctx)

    assert out[0].subcategory is None
