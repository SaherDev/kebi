"""Tests for HomeService — greeting + chips generation, cache, fail-open (ADR-111)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.config import HomeConfig
from kebi.core.home.schemas import HomeChip, HomeContext, HomeSuggestion
from kebi.core.home.service import (
    HomeService,
    daypart_for,
    weather_bucket_for,
)
from kebi.providers.llm import InstructorExtraction


def _make_service(
    *,
    extract_return: HomeSuggestion | None = None,
    extract_side_effect: Exception | None = None,
    cache_get: str | None = None,
    taste_profile: object | None = None,
    reverse_result: object | None = None,
    config: HomeConfig | None = None,
) -> tuple[HomeService, MagicMock]:
    instructor = MagicMock()
    if extract_side_effect is not None:
        instructor.extract = AsyncMock(side_effect=extract_side_effect)
    else:
        instructor.extract = AsyncMock(
            return_value=InstructorExtraction(
                data=extract_return
                or HomeSuggestion(greeting="hi", chips=[HomeChip(text="surprise me")])
            )
        )

    taste = MagicMock()
    taste.get_taste_profile = AsyncMock(return_value=taste_profile)

    geocoder = MagicMock()
    geocoder.reverse = AsyncMock(return_value=reverse_result)

    redis = MagicMock()
    redis.get = AsyncMock(return_value=cache_get)
    redis.set = AsyncMock()

    service = HomeService(
        instructor_client=instructor,
        taste_service=taste,
        geocoder=geocoder,
        redis=redis,
        config=config or HomeConfig(),
    )
    return service, redis


# ---- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (7, "morning"),
        (13, "afternoon"),
        (19, "evening"),
        (23, "late_night"),
        (2, "late_night"),
    ],
)
def test_daypart_for(hour: int, expected: str) -> None:
    assert daypart_for(datetime(2026, 6, 28, hour, tzinfo=UTC)) == expected


def test_daypart_for_none_is_anytime() -> None:
    assert daypart_for(None) == "anytime"


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("light rain", "rain"),
        ("Clear sky", "clear"),
        ("snow", "cold"),
        ("scorching hot", "hot"),
        ("foggy", "none"),
        (None, "none"),
    ],
)
def test_weather_bucket_for(hint: str | None, expected: str) -> None:
    assert weather_bucket_for(hint) == expected


# ---- generation / cache -----------------------------------------------------


async def test_cache_hit_skips_generation() -> None:
    cached = HomeSuggestion(
        greeting="cached", chips=[HomeChip(text="from cache")]
    ).model_dump_json()
    service, redis = _make_service(cache_get=cached)

    result = await service.generate("user_abc", HomeContext())

    assert result.greeting == "cached"
    service._client.extract.assert_not_awaited()  # type: ignore[attr-defined]
    redis.set.assert_not_awaited()


async def test_cache_miss_generates_and_caches() -> None:
    suggestion = HomeSuggestion(
        greeting="it's late, drunk food?",
        chips=[HomeChip(text="ramen, no line"), HomeChip(text="surprise me")],
    )
    service, redis = _make_service(extract_return=suggestion, cache_get=None)

    result = await service.generate(
        "user_abc",
        HomeContext(city="shimokitazawa", local_time=datetime(2026, 6, 28, 21, 41)),
    )

    assert result.greeting == "it's late, drunk food?"
    redis.set.assert_awaited_once()


async def test_llm_failure_returns_fallback() -> None:
    service, redis = _make_service(extract_side_effect=RuntimeError("llm down"))

    result = await service.generate("user_abc", HomeContext())

    assert result.greeting  # non-empty fallback greeting
    assert len(result.chips) >= 1
    # Fallback is NOT cached — a recovered model is picked up next open.
    redis.set.assert_not_awaited()


async def test_redis_get_failure_falls_through_to_generation() -> None:
    service, redis = _make_service(extract_return=None)
    redis.get = AsyncMock(side_effect=RuntimeError("redis down"))

    result = await service.generate("user_abc", HomeContext())

    # Did not raise; generation ran and produced a suggestion.
    assert result.greeting == "hi"


async def test_chips_trimmed_to_config_max() -> None:
    over = HomeSuggestion(
        greeting="hey",
        chips=[HomeChip(text=f"chip {i}") for i in range(6)],
    )
    service, _ = _make_service(
        extract_return=over, config=HomeConfig(chip_min=3, chip_max=4)
    )

    result = await service.generate("user_abc", HomeContext())

    assert len(result.chips) == 4


async def test_missing_location_and_weather_still_generates() -> None:
    service, _ = _make_service(extract_return=None)

    result = await service.generate("user_abc", HomeContext())  # all None

    assert result.greeting == "hi"
    # No coordinates → no reverse-geocode attempt.
    service._geocoder.reverse.assert_not_awaited()  # type: ignore[attr-defined]


async def test_reverse_geocode_used_when_only_coords_given() -> None:
    geo_result = MagicMock(city="Bangkok", neighborhood="Thonglor")
    service, _ = _make_service(extract_return=None, reverse_result=geo_result)

    await service.generate("user_abc", HomeContext(lat=13.7, lng=100.5))

    service._geocoder.reverse.assert_awaited_once()  # type: ignore[attr-defined]


async def test_taste_read_failure_does_not_break_generation() -> None:
    service, _ = _make_service(extract_return=None)
    service._taste.get_taste_profile = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("db down")
    )

    result = await service.generate("user_abc", HomeContext())

    assert result.greeting == "hi"
