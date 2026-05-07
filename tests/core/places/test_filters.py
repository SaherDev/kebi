"""Round-trip tests for the shared PlaceFilters family (feature 028 M4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kebi.core.places.filters import ConsultFilters, PlaceFilters
from kebi.core.places.models import (
    LocationContext,
    PlaceAttributes,
    PlaceSource,
    PlaceType,
)
from kebi.core.recall.types import RecallFilters


def test_place_filters_defaults_are_all_none() -> None:
    f = PlaceFilters()
    assert f.place_type is None
    assert f.subcategory is None
    assert f.tags_include is None
    assert f.attributes is None
    assert f.source is None


def test_place_filters_accepts_all_fields_populated() -> None:
    f = PlaceFilters(
        place_type=PlaceType.food_and_drink,
        subcategory="cafe",
        tags_include=["date-night"],
        attributes=PlaceAttributes(cuisine="japanese"),
        source=PlaceSource.tiktok,
    )
    assert f.place_type is PlaceType.food_and_drink
    assert f.subcategory == "cafe"
    assert f.tags_include == ["date-night"]
    assert f.attributes is not None
    assert f.attributes.cuisine == "japanese"
    assert f.source is PlaceSource.tiktok


def test_recall_filters_walks_nested_attributes() -> None:
    f = RecallFilters(
        place_type=PlaceType.food_and_drink,
        attributes=PlaceAttributes(
            cuisine="japanese",
            price_hint="mid",
            location_context=LocationContext(city="Bangkok"),
        ),
        max_distance_km=3.5,
        created_after=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert f.attributes is not None
    assert f.attributes.cuisine == "japanese"
    assert f.attributes.price_hint == "mid"
    assert f.attributes.location_context is not None
    assert f.attributes.location_context.city == "Bangkok"
    assert f.max_distance_km == 3.5


def test_recall_filters_inherits_place_filters_base() -> None:
    assert issubclass(RecallFilters, PlaceFilters)
    # Instance-level base fields are present even without extensions set.
    f = RecallFilters()
    assert f.place_type is None
    assert f.max_distance_km is None


def test_consult_filters_radius_optional() -> None:
    f = ConsultFilters()
    assert f.radius_m is None
    assert f.search_location_name is None


def test_consult_filters_accepts_geographic_bounds() -> None:
    f = ConsultFilters(radius_m=2500, search_location_name="Shibuya")
    assert f.radius_m == 2500
    assert f.search_location_name == "Shibuya"


def test_consult_filters_forbids_discovery_passthrough() -> None:
    # Provider-coupling decision: no free-form Google passthrough field.
    with pytest.raises(ValidationError):
        ConsultFilters(discovery_filters={"opennow": True})  # type: ignore[call-arg]


def test_consult_filters_inherits_place_filters() -> None:
    assert issubclass(ConsultFilters, PlaceFilters)
    f = ConsultFilters(place_type=PlaceType.accommodation)
    assert f.place_type is PlaceType.accommodation


def test_place_filters_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PlaceFilters(unknown_field="x")  # type: ignore[call-arg]


def test_recall_filters_accepts_string_enum_values() -> None:
    # PlaceType/PlaceSource inherit str; Pydantic coerces string inputs.
    f = RecallFilters(place_type="food_and_drink", source="tiktok")  # type: ignore[arg-type]
    assert f.place_type is PlaceType.food_and_drink
    assert f.source is PlaceSource.tiktok
