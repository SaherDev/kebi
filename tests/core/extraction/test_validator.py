"""Tests for GooglePlacesValidator on the evidence trail."""

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    Evidence,
    Medium,
    Producer,
    ValidatedCandidate,
)
from totoro_ai.core.extraction.validator import GooglePlacesValidator
from totoro_ai.core.places import (
    LocationContext,
    PlaceAttributes,
    PlaceProvider,
    PlaceType,
)
from totoro_ai.core.places.places_client import (
    PlacesMatchQuality,
    PlacesMatchResult,
)


def _make_config() -> ConfidenceConfig:
    return ConfidenceConfig(
        producer_scores={
            "llm_ner": 0.80,
            "vision_frames": 0.55,
            "whisper_audio": 0.65,
            "google_maps_list": 0.95,
        },
        medium_scores={
            "caption": 0.75,
            "transcript": 0.65,
            "frame": 0.55,
            "list": 0.95,
            "emoji_marker": 0.92,
        },
        corroboration_bonus=0.10,
        max_score=0.97,
    )


def _make_match(
    quality: PlacesMatchQuality = PlacesMatchQuality.EXACT,
    external_id: str | None = "place_123",
    validated_name: str = "Chez Claude",
    lat: float | None = None,
    lng: float | None = None,
    address: str | None = None,
) -> PlacesMatchResult:
    return PlacesMatchResult(
        match_quality=quality,
        validated_name=validated_name,
        external_provider="google",
        external_id=external_id,
        lat=lat,
        lng=lng,
        address=address,
    )


def _make_candidate(
    name: str = "Chez Claude",
    evidence: list[Evidence] | None = None,
    cuisine: str | None = "french",
    city: str | None = "Paris",
) -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        evidence=evidence or [Evidence(Producer.LLM_NER, Medium.CAPTION)],
        subcategory="restaurant",
        attributes=PlaceAttributes(
            cuisine=cuisine,
            location_context=LocationContext(city=city) if city else None,
        ),
    )


def _make_validator() -> GooglePlacesValidator:
    return GooglePlacesValidator(
        places_client=AsyncMock(),
        confidence_config=_make_config(),
    )


async def test_empty_candidates_returns_none() -> None:
    validator = _make_validator()
    result = await validator.validate([])
    assert result is None


async def test_single_exact_match_returns_validated_candidate() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate([_make_candidate()])

    assert results is not None
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ValidatedCandidate)
    assert r.external_id == "place_123"
    assert r.provider == PlaceProvider.google
    assert r.place_name == "Chez Claude"
    assert r.place_type == PlaceType.food_and_drink
    assert r.attributes.cuisine == "french"


async def test_evidence_propagated_through_validator() -> None:
    """The validator passes the candidate's evidence list through unchanged."""
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    evidence = [
        Evidence(Producer.LLM_NER, Medium.CAPTION, snippet="Loved Chez Claude"),
        Evidence(Producer.VISION_FRAMES, Medium.FRAME, snippet="Chez Claude"),
    ]
    results = await validator.validate([_make_candidate(evidence=evidence)])

    assert results is not None
    assert len(results) == 1
    assert results[0].evidence == evidence


async def test_validator_propagates_match_geo() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(
        PlacesMatchQuality.EXACT,
        lat=13.7563,
        lng=100.5018,
        address="1 Sukhumvit Rd, Bangkok, Thailand",
    )
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate([_make_candidate()])

    assert results is not None
    r = results[0]
    assert r.match_lat == 13.7563
    assert r.match_lng == 100.5018
    assert r.match_address == "1 Sukhumvit Rd, Bangkok, Thailand"


async def test_validator_passes_city_from_location_context() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    await validator.validate([_make_candidate(city="Tokyo")])

    client.validate_place.assert_awaited_once()
    call_kwargs = client.validate_place.await_args.kwargs
    assert call_kwargs["name"] == "Chez Claude"
    assert call_kwargs["location"] == "Tokyo"


async def test_validator_passes_none_location_when_no_city() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    await validator.validate([_make_candidate(city=None)])

    assert client.validate_place.await_args.kwargs["location"] is None


async def test_multi_evidence_corroboration_bonus_applied() -> None:
    """Two distinct (producer, medium) pairs → corroboration bonus."""
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    evidence = [
        Evidence(Producer.LLM_NER, Medium.CAPTION),
        Evidence(Producer.VISION_FRAMES, Medium.FRAME),
    ]
    results = await validator.validate([_make_candidate(evidence=evidence)])

    assert results is not None
    # base = max(0.80, 0.75) = 0.80; *1.0 + 0.10 = 0.90, capped at 0.97.
    assert results[0].confidence > 0.85


async def test_none_match_low_confidence() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.NONE)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate([_make_candidate()])

    assert results is not None
    # base 0.80 * 0.30 = 0.24, no bonus (single evidence).
    assert results[0].confidence < 0.30


async def test_all_none_external_id_returns_none() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(
        PlacesMatchQuality.NONE, external_id=None
    )
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    result = await validator.validate(
        [_make_candidate(), _make_candidate(name="Bistro B")]
    )
    assert result is None


async def test_five_candidates_validated_in_parallel() -> None:
    call_order: list[str] = []

    async def fake_validate(
        name: str, location: str | None = None
    ) -> PlacesMatchResult:
        call_order.append(name)
        return _make_match(external_id=f"id_{name}")

    client = MagicMock()
    client.validate_place = AsyncMock(side_effect=fake_validate)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    candidates = [_make_candidate(name=f"Place {i}") for i in range(5)]
    results = await validator.validate(candidates)

    assert client.validate_place.call_count == 5
    assert results is not None
    assert len(results) == 5
    assert set(call_order) == {f"Place {i}" for i in range(5)}


async def test_runtime_error_on_one_does_not_crash_batch() -> None:
    good_match = _make_match(external_id="good_id", validated_name="Good Place")
    call_count = 0

    async def fake_validate(
        name: str, location: str | None = None
    ) -> PlacesMatchResult:
        nonlocal call_count
        call_count += 1
        if name == "Bad Place":
            raise RuntimeError("Google Places API error")
        return good_match

    client = MagicMock()
    client.validate_place = AsyncMock(side_effect=fake_validate)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    candidates = [
        _make_candidate(name="Good Place"),
        _make_candidate(name="Bad Place"),
        _make_candidate(name="Another Good"),
    ]
    results = await validator.validate(candidates)

    assert results is not None
    assert len(results) == 2
    assert call_count == 3
