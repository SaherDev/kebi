"""Tests for GooglePlacesValidator (ADR-054 / feature 019)."""

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ExtractionLevel,
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
        base_scores={
            "emoji_regex": 0.95,
            "llm_ner": 0.80,
            "subtitle_check": 0.75,
            "whisper_audio": 0.65,
            "vision_frames": 0.55,
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
    source: ExtractionLevel = ExtractionLevel.EMOJI_REGEX,
    corroborated: bool = False,
    cuisine: str | None = "french",
    city: str | None = "Paris",
) -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        source=source,
        subcategory="restaurant",
        attributes=PlaceAttributes(
            cuisine=cuisine,
            location_context=LocationContext(city=city) if city else None,
        ),
        corroborated=corroborated,
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
    # confidence = min(0.95 * 1.0 + 0.0, 0.97) = 0.95
    assert abs(r.confidence - 0.95) < 1e-9
    assert r.external_id == "place_123"
    assert r.provider == PlaceProvider.google
    assert r.place_name == "Chez Claude"
    assert r.place_type == PlaceType.food_and_drink
    assert r.attributes.cuisine == "french"
    assert r.attributes.location_context is not None
    assert r.attributes.location_context.city == "Paris"


async def test_validator_propagates_match_geo_onto_validated_candidate() -> None:
    """Lat/lng/address from Google validation must reach the persistence
    layer via ValidatedCandidate so the geo cache write has data to use."""
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
    assert len(results) == 1
    r = results[0]
    assert r.match_lat == 13.7563
    assert r.match_lng == 100.5018
    assert r.match_address == "1 Sukhumvit Rd, Bangkok, Thailand"


async def test_validator_passes_city_from_location_context_to_client() -> None:
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


async def test_fuzzy_match_uses_modifier_0_9() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.FUZZY)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate(
        [_make_candidate(source=ExtractionLevel.LLM_NER)]
    )

    assert results is not None
    # confidence = min(0.80 * 0.9 + 0.0, 0.97) = 0.72
    assert abs(results[0].confidence - 0.72) < 1e-9


async def test_none_match_uses_modifier_0_3() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.NONE)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate(
        [_make_candidate(source=ExtractionLevel.LLM_NER)]
    )

    assert results is not None
    # confidence = min(0.80 * 0.3 + 0.0, 0.97) = 0.24
    assert abs(results[0].confidence - 0.24) < 1e-9


async def test_corroborated_candidate_gets_bonus() -> None:
    client = AsyncMock()
    client.validate_place.return_value = _make_match(PlacesMatchQuality.EXACT)
    validator = GooglePlacesValidator(
        places_client=client, confidence_config=_make_config()
    )

    results = await validator.validate([_make_candidate(corroborated=True)])

    assert results is not None
    # confidence = min(0.95 * 1.0 + 0.10, 0.97) = 0.97
    assert abs(results[0].confidence - 0.97) < 1e-9


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
