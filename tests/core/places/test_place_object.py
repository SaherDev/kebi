"""Shape-only tests for the Pydantic models in core/places/models.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kebi.core.places.models import (
    DuplicatePlaceError,
    DuplicateProviderId,
    GeoData,
    HoursDict,
    LocationContext,
    PlaceAttributes,
    PlaceCreate,
    PlaceEnrichment,
    PlaceObject,
    PlaceProvider,
    PlaceSource,
    PlaceType,
)

# ---------------------------------------------------------------------------
# PlaceObject defaults
# ---------------------------------------------------------------------------


def test_place_object_tier1_only_defaults_freshness_flags_false() -> None:
    obj = PlaceObject(
        place_id="pid-123",
        place_name="Test Cafe",
        place_type=PlaceType.food_and_drink,
    )
    assert obj.geo_fresh is False
    assert obj.enriched is False
    assert obj.lat is None
    assert obj.lng is None
    assert obj.address is None
    assert obj.hours is None
    assert obj.rating is None
    assert obj.subcategory is None
    assert obj.tags == []
    assert obj.source is None
    assert obj.provider_id is None


def test_place_attributes_defaults_to_none_and_empty_lists() -> None:
    attrs = PlaceAttributes()
    assert attrs.cuisine is None
    assert attrs.price_hint is None
    assert attrs.ambiance is None
    assert attrs.dietary == []
    assert attrs.good_for == []
    assert attrs.location_context is None


def test_place_object_attributes_default_factory_is_isolated_per_instance() -> None:
    # Guards against the classic Pydantic "mutable default shared across instances".
    a = PlaceObject(place_id="a", place_name="A", place_type=PlaceType.food_and_drink)
    b = PlaceObject(place_id="b", place_name="B", place_type=PlaceType.food_and_drink)
    a.tags.append("date-night")
    a.attributes.dietary.append("vegan")
    assert b.tags == []
    assert b.attributes.dietary == []


# ---------------------------------------------------------------------------
# HoursDict round-trip through Pydantic JSON
# ---------------------------------------------------------------------------


def test_hours_dict_with_timezone_round_trips_through_enrichment_json() -> None:
    hours: HoursDict = {
        "monday": "09:00-22:00",
        "tuesday": None,  # closed
        "timezone": "Asia/Tokyo",
    }
    enr = PlaceEnrichment(
        hours=hours,
        rating=4.5,
        phone="+81-3-1234-5678",
        photo_url=None,
        popularity=0.8,
        fetched_at=datetime(2026, 4, 14, tzinfo=UTC),
    )
    payload = enr.model_dump_json()
    assert '"timezone":"Asia/Tokyo"' in payload
    restored = PlaceEnrichment.model_validate_json(payload)
    assert restored.hours is not None
    assert restored.hours.get("timezone") == "Asia/Tokyo"
    assert restored.hours.get("monday") == "09:00-22:00"
    assert restored.hours.get("tuesday") is None
    assert restored.rating == 4.5


def test_geo_data_round_trips_through_json() -> None:
    geo = GeoData(
        lat=35.6762,
        lng=139.6503,
        address="Tokyo, Japan",
        cached_at=datetime(2026, 4, 14, tzinfo=UTC),
    )
    restored = GeoData.model_validate_json(geo.model_dump_json())
    assert restored.lat == geo.lat
    assert restored.lng == geo.lng
    assert restored.address == geo.address


# ---------------------------------------------------------------------------
# PlaceCreate validation
# ---------------------------------------------------------------------------


def test_place_create_with_provider_and_external_id_constructs() -> None:
    pc = PlaceCreate(
        user_id="u1",
        place_name="Blue Bottle",
        place_type=PlaceType.food_and_drink,
        subcategory="cafe",
        tags=["hidden-gem"],
        attributes=PlaceAttributes(cuisine="american", price_hint="moderate"),
        source=PlaceSource.manual,
        external_id="ChIJN1t_tDeuEmsRUsoyG83frY4",
        provider=PlaceProvider.google,
    )
    assert pc.external_id == "ChIJN1t_tDeuEmsRUsoyG83frY4"
    assert pc.provider == PlaceProvider.google


def test_place_create_with_neither_provider_nor_external_id_constructs() -> None:
    pc = PlaceCreate(
        user_id="u1",
        place_name="Nameless stall",
        place_type=PlaceType.food_and_drink,
    )
    assert pc.provider is None
    assert pc.external_id is None


def test_place_create_with_provider_but_no_external_id_raises() -> None:
    with pytest.raises(ValidationError, match="external_id and provider must be both"):
        PlaceCreate(
            user_id="u1",
            place_name="x",
            place_type=PlaceType.food_and_drink,
            provider=PlaceProvider.google,
        )


def test_place_create_with_external_id_but_no_provider_raises() -> None:
    with pytest.raises(ValidationError, match="external_id and provider must be both"):
        PlaceCreate(
            user_id="u1",
            place_name="x",
            place_type=PlaceType.food_and_drink,
            external_id="ChIJ_something",
        )


def test_place_create_with_invalid_subcategory_for_type_raises() -> None:
    with pytest.raises(ValidationError, match="subcategory 'cafe' is not valid"):
        PlaceCreate(
            user_id="u1",
            place_name="x",
            place_type=PlaceType.shopping,
            subcategory="cafe",  # not in shopping vocabulary
        )


def test_place_create_with_valid_subcategory_for_type_constructs() -> None:
    pc = PlaceCreate(
        user_id="u1",
        place_name="x",
        place_type=PlaceType.shopping,
        subcategory="boutique",
    )
    assert pc.subcategory == "boutique"


def test_place_create_empty_place_name_rejected() -> None:
    with pytest.raises(ValidationError):
        PlaceCreate(
            user_id="u1",
            place_name="",
            place_type=PlaceType.food_and_drink,
        )


def test_place_create_empty_user_id_rejected() -> None:
    with pytest.raises(ValidationError):
        PlaceCreate(
            user_id="",
            place_name="x",
            place_type=PlaceType.food_and_drink,
        )


# ---------------------------------------------------------------------------
# LocationContext attribute nesting
# ---------------------------------------------------------------------------


def test_attributes_can_carry_location_context() -> None:
    attrs = PlaceAttributes(
        cuisine="japanese",
        location_context=LocationContext(
            neighborhood="Shibuya", city="Tokyo", country="JP"
        ),
    )
    payload = attrs.model_dump_json()
    restored = PlaceAttributes.model_validate_json(payload)
    assert restored.location_context is not None
    assert restored.location_context.city == "Tokyo"


# ---------------------------------------------------------------------------
# DuplicatePlaceError shape
# ---------------------------------------------------------------------------


def test_duplicate_place_error_carries_conflicts_list() -> None:
    c1 = DuplicateProviderId(provider_id="google:ChIJ_aaa", existing_place_id="pid-1")
    c2 = DuplicateProviderId(provider_id="google:ChIJ_bbb", existing_place_id="pid-2")
    err = DuplicatePlaceError([c1, c2])
    assert err.conflicts == [c1, c2]
    assert "google:ChIJ_aaa" in str(err)
    assert "google:ChIJ_bbb" in str(err)


def test_duplicate_place_error_single_conflict() -> None:
    c = DuplicateProviderId(
        provider_id="google:ChIJ_xxx", existing_place_id="pid-existing"
    )
    err = DuplicatePlaceError([c])
    assert len(err.conflicts) == 1
    assert err.conflicts[0].existing_place_id == "pid-existing"
