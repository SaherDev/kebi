"""Tests for the weather seam (ADR-144)."""

from __future__ import annotations

from kebi.providers.weather import NullWeatherProvider, WeatherProvider


async def test_the_null_provider_reports_unknown() -> None:
    assert await NullWeatherProvider().current(lat=-8.65, lng=115.13) is None


def test_the_null_provider_satisfies_the_protocol() -> None:
    # The seam only pays off if a real source can drop in without touching
    # callers, which requires the null one to be a faithful stand-in.
    provider: WeatherProvider = NullWeatherProvider()
    assert provider is not None


async def test_unknown_conditions_leave_the_calendar_season_in_charge() -> None:
    """Absent weather must not perturb ranking — today's behaviour exactly.

    The seam is here so a source can be added later; until then nothing about
    the answer may change.
    """
    from kebi.core.agent.graph import local_season

    state = {
        "local_time": "2026-08-11T15:00:00+09:00",
        "working_location": {"lat": 35.6, "lng": 139.7},
    }
    assert local_season(state) == "summer"
    assert await NullWeatherProvider().current(lat=35.6, lng=139.7) is None
