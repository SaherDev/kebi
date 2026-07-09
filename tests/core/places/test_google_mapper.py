"""Tests for the Google → PlaceObject mapper (ADR-118 minimal-validator scope)."""

from __future__ import annotations

from datetime import UTC, datetime

from kebi.core.places._google_mapper import map_place
from kebi.core.places.models import PlaceCategory
from kebi.core.places.tags import CuisineTag, TagType

_NOW = datetime(2026, 7, 9, tzinfo=UTC)

# A raw payload in the OLD (pre-ADR-118) full-mask shape. The mapper must
# ignore every experiential/live field even if a response carries them.
_LEGACY_FULL_PAYLOAD: dict[str, object] = {
    "id": "ChIJfull",
    "displayName": {"text": "Nara Eatery"},
    "formattedAddress": "1 Sukhumvit, Bangkok",
    "addressComponents": [
        {"longText": "Bangkok", "types": ["locality"]},
        {"longText": "Thailand", "types": ["country"]},
    ],
    "location": {"latitude": 13.7, "longitude": 100.5},
    "types": ["thai_restaurant", "restaurant", "food"],
    "priceLevel": "PRICE_LEVEL_MODERATE",
    "rating": 4.6,
    "userRatingCount": 1200,
    "regularOpeningHours": {"periods": [{"open": {"day": 1, "hour": 9}}]},
    "timeZone": {"id": "Asia/Bangkok"},
    "nationalPhoneNumber": "+66 2 555 0000",
    "websiteUri": "https://nara.example",
    "businessStatus": "OPERATIONAL",
    "dineIn": True,
    "outdoorSeating": True,
    "servesVegetarianFood": True,
    "accessibilityOptions": {"wheelchairAccessibleEntrance": True},
}


class TestMinimalValidatorScope:
    def test_only_types_derived_tags_survive(self) -> None:
        obj = map_place(dict(_LEGACY_FULL_PAYLOAD), _NOW)

        assert obj is not None
        assert obj.place_name == "Nara Eatery"
        assert PlaceCategory.restaurant in obj.categories
        # types-derived cuisine tag survives...
        assert [(t.type, t.value) for t in obj.tags] == [
            (TagType.cuisine, CuisineTag.thai)
        ]
        # ...and every tag is provider-attested.
        assert all(t.source == "google" for t in obj.tags)

    def test_no_live_fields_on_the_object(self) -> None:
        obj = map_place(dict(_LEGACY_FULL_PAYLOAD), _NOW)

        assert obj is not None
        for gone in ("rating", "hours", "phone", "website", "popularity"):
            assert not hasattr(obj, gone)

    def test_dietary_from_types_survives(self) -> None:
        raw = dict(_LEGACY_FULL_PAYLOAD, types=["vegan_restaurant", "restaurant"])
        obj = map_place(raw, _NOW)

        assert obj is not None
        values = {(t.type, t.value) for t in obj.tags}
        assert ("dietary", "vegan") in values
        assert ("dietary", "vegetarian") in values

    def test_location_and_address_mapping(self) -> None:
        obj = map_place(dict(_LEGACY_FULL_PAYLOAD), _NOW)

        assert obj is not None
        assert obj.location is not None
        assert obj.location.lat == 13.7
        assert obj.location.address == "1 Sukhumvit, Bangkok"
        assert obj.location.city == "Bangkok"
        assert obj.location.country == "Thailand"
        assert obj.cached_at == _NOW


class TestRequireName:
    def test_search_mode_rejects_nameless(self) -> None:
        raw = dict(_LEGACY_FULL_PAYLOAD)
        del raw["displayName"]
        assert map_place(raw, _NOW) is None

    def test_details_mode_accepts_nameless(self) -> None:
        raw = dict(_LEGACY_FULL_PAYLOAD)
        del raw["displayName"]
        obj = map_place(raw, _NOW, require_name=False)

        assert obj is not None
        assert obj.place_name == ""
        assert obj.provider_id == "google:ChIJfull"

    def test_missing_id_rejected_in_both_modes(self) -> None:
        raw = dict(_LEGACY_FULL_PAYLOAD)
        del raw["id"]
        assert map_place(raw, _NOW) is None
        assert map_place(raw, _NOW, require_name=False) is None
