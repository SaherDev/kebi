"""Tests for the WorkingLocation and LocationResolution models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.core.agent.location import LocationResolution, WorkingLocation


def test_working_location_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        WorkingLocation(city="Tokyo", lat=35.6, lng=139.7)  # type: ignore[call-arg]


def test_working_location_neighborhood_is_optional() -> None:
    wl = WorkingLocation(country="Japan", city="Tokyo", lat=35.6, lng=139.7)
    assert wl.neighborhood is None


def test_working_location_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WorkingLocation(
            country="Japan",
            city="Tokyo",
            lat=35.6,
            lng=139.7,
            region="Kanto",  # type: ignore[call-arg]
        )


def test_location_resolution_accepts_partial_with_flags() -> None:
    r = LocationResolution(
        source="explicit_query",
        city="Cambridge",
        is_ambiguous=True,
        needs_clarification=True,
        clarification_reason="UK or Massachusetts?",
    )
    assert r.country is None
    assert r.is_ambiguous is True


def test_location_resolution_requires_source() -> None:
    with pytest.raises(ValidationError):
        LocationResolution()  # type: ignore[call-arg]
