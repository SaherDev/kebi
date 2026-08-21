"""HomeService — generate the home greeting + chips, cached (ADR-111).

One Instructor call turns the user's taste signal plus client-supplied local
context (location, local time, optional weather) into a short greeting and a
few tappable intent chips. The payload is Redis-cached per user keyed by a
*coarse* context bucket (daypart, rough location, weather band, taste version),
so most home-screen opens are a cache hit and a taste regeneration naturally
refreshes the suggestions.

Fail-open everywhere: any geocoding, cache, or LLM error degrades to a static
neutral greeting + generic chips — the home screen must always render.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, cast

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import HomeConfig, get_prompt
from kebi.core.home.schemas import HomeChip, HomeContext, HomeSuggestion
from kebi.core.prompt_safety import wrap_untrusted
from kebi.core.taste.regen import format_summary_for_agent
from kebi.core.taste.schemas import SummaryLine

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from kebi.core.places.nominatim_geocoding_client import (
        NominatimGeocodingClient,
    )
    from kebi.core.taste.schemas import TasteProfile
    from kebi.core.taste.service import TasteModelService
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)

_KEY_PREFIX = "home:v1:"

# Static fallback when generation can't run — the home screen always renders.
_FALLBACK_GREETING = "where to next?"
_FALLBACK_CHIPS = [
    "somewhere nearby",
    "a place i'd like",
    "something new",
    "surprise me",
]


def daypart_for(local_time: datetime | None) -> str:
    """Coarse part-of-day from the client's local time (its timezone is
    canonical). 'anytime' when unknown so the greeting stays neutral."""
    if local_time is None:
        return "anytime"
    hour = local_time.hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "late_night"


def weather_bucket_for(weather: str | None) -> str:
    """Map a free-text client weather hint to a coarse band for the cache key
    and prompt. 'none' when absent or unrecognized."""
    if not weather:
        return "none"
    w = weather.lower()
    if any(t in w for t in ("rain", "drizzle", "storm", "shower", "thunder")):
        return "rain"
    if any(t in w for t in ("snow", "sleet", "cold", "freez")):
        return "cold"
    if any(t in w for t in ("hot", "heat", "scorch")):
        return "hot"
    if any(t in w for t in ("clear", "sun", "fair")):
        return "clear"
    return "none"


def _slug(value: str | None) -> str:
    """Lowercase, hyphenated slug for a cache-key segment; 'none' when empty."""
    if not value:
        return "none"
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "none"


class HomeService:
    def __init__(
        self,
        instructor_client: InstructorClient,
        taste_service: TasteModelService,
        geocoder: NominatimGeocodingClient,
        redis: Redis,
        config: HomeConfig,
    ) -> None:
        self._client = instructor_client
        self._taste = taste_service
        self._geocoder = geocoder
        self._redis = redis
        self._config = config

    async def generate(self, user_id: str, context: HomeContext) -> HomeSuggestion:
        """Greeting + chips for the caller — cache-first, generate on miss.

        Never raises: any failure path returns the static fallback.
        """
        daypart = daypart_for(context.local_time)
        weather_bucket = weather_bucket_for(context.weather)
        city, neighborhood = await self._resolve_place(context)

        profile = await self._read_taste(user_id)
        taste_fp = profile.generated_from_log_count if profile else 0

        key = self._cache_key(user_id, city, daypart, weather_bucket, taste_fp)
        cached = await self._cache_get(key)
        if cached is not None:
            return cached

        suggestion = await self._generate(
            user_id,
            taste_summary=self._taste_summary(profile),
            city=city,
            neighborhood=neighborhood,
            local_time=context.local_time,
            daypart=daypart,
            weather_bucket=weather_bucket,
        )
        if suggestion is None:
            # Generation failed — serve the static fallback but do NOT cache
            # it, so a recovered model is picked up on the next open.
            return self._fallback()
        await self._cache_set(key, suggestion)
        return suggestion

    # ---- context resolution -------------------------------------------------

    async def _resolve_place(
        self, context: HomeContext
    ) -> tuple[str | None, str | None]:
        """Resolve (city, neighborhood). Prefer the client's city; otherwise
        reverse-geocode coordinates. Fail-open to (None, None)."""
        if context.city:
            return context.city, None
        if context.lat is None or context.lng is None:
            return None, None
        try:
            result = await self._geocoder.reverse(lat=context.lat, lng=context.lng)
        except Exception:
            logger.warning("home reverse-geocode failed", exc_info=True)
            return None, None
        if result is None:
            return None, None
        return result.city, result.neighborhood

    async def _read_taste(self, user_id: str) -> TasteProfile | None:
        try:
            return await self._taste.get_taste_profile(user_id)
        except Exception:
            logger.warning("home taste read failed", exc_info=True)
            return None

    @staticmethod
    def _taste_summary(profile: TasteProfile | None) -> str:
        if profile is None or not profile.taste_profile_summary:
            return ""
        lines = [
            SummaryLine.model_validate(item) if isinstance(item, dict) else item
            for item in profile.taste_profile_summary
        ]
        return format_summary_for_agent(lines)

    # ---- generation ---------------------------------------------------------

    async def _generate(
        self,
        user_id: str,
        *,
        taste_summary: str,
        city: str | None,
        neighborhood: str | None,
        local_time: datetime | None,
        daypart: str,
        weather_bucket: str,
    ) -> HomeSuggestion | None:
        """One Instructor call → suggestion, or None on any failure (the
        caller serves the static fallback)."""
        prompt = get_prompt("home_suggester").format(
            taste_block=self._taste_block(taste_summary),
            location_block=self._location_block(city, neighborhood),
            time_block=self._time_block(local_time, daypart),
            weather_block=weather_bucket if weather_bucket != "none" else "(none)",
            chip_min=self._config.chip_min,
            chip_max=self._config.chip_max,
        )
        try:
            async with traced_call(
                "home_suggester.llm",
                "home",
                role="home_suggester",
                user_id=user_id,
                standalone=True,
                input={"daypart": daypart, "city": city or ""},
            ) as t:
                extraction = await self._client.extract(
                    response_model=HomeSuggestion,
                    messages=[{"role": "user", "content": prompt}],
                )
                t.usage = extraction.usage
                t.attempts = extraction.attempts
                result = cast(HomeSuggestion, extraction.data)
                # Honor the configured max; the prompt guides the min.
                result = HomeSuggestion(
                    greeting=result.greeting,
                    chips=result.chips[: self._config.chip_max],
                )
                t.output = {"chips": len(result.chips)}
                return result
        except Exception:
            logger.warning("home suggestion generation failed", exc_info=True)
            return None

    def _fallback(self) -> HomeSuggestion:
        chips = _FALLBACK_CHIPS[: max(self._config.chip_min, 1)]
        return HomeSuggestion(
            greeting=_FALLBACK_GREETING,
            chips=[HomeChip(text=c) for c in chips],
        )

    @staticmethod
    def _taste_block(taste_summary: str) -> str:
        summary = (taste_summary or "").strip()
        if not summary:
            return "(no prior taste signal — treat as a cold start)"
        return wrap_untrusted(summary, "taste_profile")

    @staticmethod
    def _location_block(city: str | None, neighborhood: str | None) -> str:
        if not city and not neighborhood:
            return "(location unknown)"
        parts: list[str] = []
        if neighborhood:
            parts.append(f"Neighborhood: {neighborhood}")
        if city:
            parts.append(f"City: {city}")
        return "\n".join(parts)

    @staticmethod
    def _time_block(local_time: datetime | None, daypart: str) -> str:
        if local_time is None:
            return f"Part of day: {daypart}"
        weekend = local_time.weekday() >= 5
        return (
            f"Local time: {local_time.strftime('%H:%M')}\n"
            f"Part of day: {daypart}\n"
            f"Day: {'weekend' if weekend else 'weekday'}"
        )

    # ---- cache (fail-open) --------------------------------------------------

    @staticmethod
    def _cache_key(
        user_id: str,
        city: str | None,
        daypart: str,
        weather_bucket: str,
        taste_fp: int,
    ) -> str:
        return (
            f"{_KEY_PREFIX}{user_id}:{_slug(city)}:{daypart}:"
            f"{weather_bucket}:{taste_fp}"
        )

    async def _cache_get(self, key: str) -> HomeSuggestion | None:
        try:
            raw = await self._redis.get(key)
        except Exception:
            logger.warning("home cache get failed", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return HomeSuggestion.model_validate_json(raw)
        except Exception:
            logger.warning("home cache decode failed — treating as miss")
            return None

    async def _cache_set(self, key: str, suggestion: HomeSuggestion) -> None:
        try:
            await self._redis.set(
                key,
                suggestion.model_dump_json(),
                ex=self._config.cache_ttl_seconds,
            )
        except Exception:
            logger.warning("home cache set failed", exc_info=True)
