"""Tests for the Google → PlaceObject mapper (ADR-118 minimal-validator scope)."""

from __future__ import annotations

from datetime import UTC, datetime

from kebi.core.places._google_mapper import is_non_venue_geography, map_place
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


class TestAdministrativeAreaReject:
    """ADR-082: a search matching an administrative area (a town, district,
    road) is not a savable place and is dropped at validation."""

    @staticmethod
    def _raw(name: str, types: list[str]) -> dict[str, object]:
        return {
            "id": "ChIJarea",
            "displayName": {"text": name},
            "location": {"latitude": 16.05, "longitude": 108.2},
            "types": types,
        }

    def test_locality_rejected_in_search_mode(self) -> None:
        raw = self._raw("Hoi An", ["locality", "political"])
        assert map_place(raw, _NOW) is None

    def test_road_rejected_in_search_mode(self) -> None:
        raw = self._raw("Sukhumvit Road", ["route"])
        assert map_place(raw, _NOW) is None

    def test_admin_area_rejected_in_search_mode(self) -> None:
        raw = self._raw("Đà Nẵng", ["administrative_area_level_1", "political"])
        assert map_place(raw, _NOW) is None

    def test_details_mode_keeps_administrative_result(self) -> None:
        """The by-id refresh maps its already-catalogued venue; it never
        re-classifies, so the reject is search-only."""
        raw = self._raw("Hoi An", ["locality", "political"])
        obj = map_place(raw, _NOW, require_name=False)
        assert obj is not None
        assert obj.place_name == "Hoi An"

    def test_real_venue_type_survives(self) -> None:
        raw = self._raw("Nara Eatery", ["thai_restaurant", "restaurant", "food"])
        obj = map_place(raw, _NOW)
        assert obj is not None
        assert PlaceCategory.restaurant in obj.categories

    def test_district_as_attraction_survives(self) -> None:
        """A place Google itself classifies as a venue keeps its category and
        is NOT dropped, even alongside a `political` type (ADR-082 carve-out)."""
        raw = self._raw(
            "Zaanse Schans", ["tourist_attraction", "point_of_interest", "political"]
        )
        obj = map_place(raw, _NOW)
        assert obj is not None
        assert obj.categories  # mapped from tourist_attraction

    def test_empty_types_not_rejected(self) -> None:
        """A niche venue with no Google type (empty types) is kept — the reject
        requires a positive administrative signal, not mere category-emptiness."""
        raw = self._raw("Some Hidden Bar", [])
        obj = map_place(raw, _NOW)
        assert obj is not None
        assert obj.categories == []


class TestNonVenueGeographyReject:
    """Location-kinds Step 1: linear/natural geography co-stamped
    `tourist_attraction` no longer passes as a venue — the exact hole
    that let "Ha Giang Loop" save as a landmark."""

    @staticmethod
    def _raw(name: str, types: list[str]) -> dict[str, object]:
        return {
            "id": "ChIJgeo",
            "displayName": {"text": name},
            "location": {"latitude": 22.8, "longitude": 105.0},
            "types": types,
        }

    # -- the rule itself ------------------------------------------------

    def test_route_with_tourist_attraction_is_non_venue(self) -> None:
        """Incident 1: Ha Giang Loop."""
        assert is_non_venue_geography(["tourist_attraction", "route"]) is True

    def test_natural_feature_with_tourist_attraction_is_non_venue(self) -> None:
        """Incident 2 shape: Hai Van Pass, and Mount Fuji / Ha Long Bay —
        accepted Step 1 behavior; they become areas in Step 2."""
        assert is_non_venue_geography(["tourist_attraction", "natural_feature"]) is True

    def test_bare_natural_feature_is_non_venue(self) -> None:
        """The roadmap's literal example: previously leaked with empty
        categories because no administrative type matched."""
        assert is_non_venue_geography(["natural_feature", "point_of_interest"]) is True

    def test_tourist_attraction_alone_is_venue(self) -> None:
        """Burj Khalifa: generic attraction with no geography marker."""
        assert (
            is_non_venue_geography(["tourist_attraction", "point_of_interest"]) is False
        )

    def test_specific_venue_type_wins_over_geography_marker(self) -> None:
        """Bondi Beach (`beach`), Hoan Kiem Lake (`lake`): a specific venue
        type keeps the place a venue even beside natural_feature."""
        assert (
            is_non_venue_geography(["beach", "natural_feature", "tourist_attraction"])
            is False
        )
        assert is_non_venue_geography(["lake", "natural_feature"]) is False

    def test_admin_type_without_attraction_is_non_venue(self) -> None:
        assert is_non_venue_geography(["locality", "political"]) is True

    def test_district_as_attraction_carve_out_holds(self) -> None:
        """Zaanse Schans: political + tourist_attraction stays a venue
        (ADR-124 carve-out)."""
        assert (
            is_non_venue_geography(
                ["tourist_attraction", "point_of_interest", "political"]
            )
            is False
        )

    def test_empty_types_is_venue(self) -> None:
        assert is_non_venue_geography([]) is False

    # -- the gate in map_place ------------------------------------------

    def test_route_attraction_rejected_in_search_mode(self) -> None:
        raw = self._raw("Ha Giang Loop", ["tourist_attraction", "route"])
        assert map_place(raw, _NOW) is None

    def test_natural_feature_attraction_rejected_in_search_mode(self) -> None:
        raw = self._raw("Hai Van Pass", ["tourist_attraction", "natural_feature"])
        assert map_place(raw, _NOW) is None

    def test_details_mode_never_gates(self) -> None:
        """The by-id TTL refresh must keep mapping an already-catalogued
        row even if its types would fail search validation."""
        raw = self._raw("Ha Giang Loop", ["tourist_attraction", "route"])
        obj = map_place(raw, _NOW, require_name=False)
        assert obj is not None

    def test_nature_venue_with_specific_type_survives_search(self) -> None:
        raw = self._raw(
            "Bondi Beach", ["beach", "natural_feature", "tourist_attraction"]
        )
        obj = map_place(raw, _NOW)
        assert obj is not None
        assert PlaceCategory.beach in obj.categories


class TestAddressComponentFallback:
    """Ranked fallback (ADR-119): municipality-style cities and district
    neighborhoods populate even without a `locality` component."""

    @staticmethod
    def _raw_with_components(components: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": "ChIJaddr",
            "displayName": {"text": "Addr Test"},
            "location": {"latitude": 1.0, "longitude": 2.0},
            "types": [],
            "addressComponents": components,
        }

    def test_country_code_from_short_text(self) -> None:
        """The country component's shortText populates the ISO alpha-2
        country_code (ADR-121), lowercased, alongside the display country."""
        raw = self._raw_with_components(
            [
                {"longText": "Dubai", "types": ["locality"]},
                {
                    "longText": "United Arab Emirates",
                    "shortText": "AE",
                    "types": ["country", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)
        assert obj is not None and obj.location is not None
        assert obj.location.country == "United Arab Emirates"
        assert obj.location.country_code == "ae"

    def test_country_code_none_without_short_text(self) -> None:
        raw = self._raw_with_components(
            [{"longText": "Thailand", "types": ["country"]}]
        )
        obj = map_place(raw, _NOW)
        assert obj is not None and obj.location is not None
        assert obj.location.country_code is None

    def test_da_nang_shape_admin_levels_fall_back(self) -> None:
        """Real shape from google:ChIJ2y5L4dQNQjER... (The Marble Mountains)."""
        raw = self._raw_with_components(
            [
                {"longText": "81", "types": ["street_number"]},
                {"longText": "Huyền Trân Công Chúa", "types": ["route"]},
                {
                    "longText": "Ngũ Hành Sơn",
                    "types": ["administrative_area_level_2", "political"],
                },
                {
                    "longText": "Đà Nẵng",
                    "types": ["administrative_area_level_1", "political"],
                },
                {"longText": "Vietnam", "types": ["country", "political"]},
                {"longText": "550000", "types": ["postal_code"]},
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "Đà Nẵng"
        assert obj.location.neighborhood == "Ngũ Hành Sơn"
        assert obj.location.country == "Vietnam"

    def test_locality_beats_admin_level_1_regardless_of_order(self) -> None:
        """US shape: the state appears FIRST in the array but locality wins —
        rank beats response order."""
        raw = self._raw_with_components(
            [
                {
                    "longText": "California",
                    "types": ["administrative_area_level_1", "political"],
                },
                {"longText": "San Francisco", "types": ["locality", "political"]},
                {"longText": "United States", "types": ["country", "political"]},
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "San Francisco"

    def test_uk_postal_town_falls_back(self) -> None:
        raw = self._raw_with_components(
            [
                {"longText": "London", "types": ["postal_town"]},
                {
                    "longText": "Greater London",
                    "types": ["administrative_area_level_2", "political"],
                },
                {"longText": "United Kingdom", "types": ["country", "political"]},
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "London"
        assert obj.location.neighborhood == "Greater London"

    def test_sublocality_beats_admin_level_2(self) -> None:
        raw = self._raw_with_components(
            [
                {
                    "longText": "Khet Watthana",
                    "types": ["administrative_area_level_2", "political"],
                },
                {
                    "longText": "Khlong Toei Nuea",
                    "types": ["sublocality_level_1", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.neighborhood == "Khlong Toei Nuea"

    def test_no_city_like_component_stays_none(self) -> None:
        raw = self._raw_with_components(
            [{"longText": "42", "types": ["street_number"]}]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city is None
        assert obj.location.neighborhood is None

    def test_empty_long_text_is_skipped(self) -> None:
        raw = self._raw_with_components([{"longText": "", "types": ["locality"]}])
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city is None

    def test_japan_shape_sublocality_level_2_is_neighborhood(self) -> None:
        """Real shape from google:ChIJ8T1GpMGOGGAR... (Sensō-ji): the district
        (Asakusa) is sublocality_level_2; chōme/block levels stay unmapped."""
        raw = self._raw_with_components(
            [
                {"longText": "1", "types": ["premise"]},
                {"longText": "3", "types": ["sublocality_level_4", "sublocality"]},
                {
                    "longText": "2-chōme",
                    "types": ["sublocality_level_3", "sublocality"],
                },
                {
                    "longText": "Asakusa",
                    "types": ["sublocality_level_2", "sublocality"],
                },
                {"longText": "Taito City", "types": ["locality", "political"]},
                {
                    "longText": "Tokyo",
                    "types": ["administrative_area_level_1", "political"],
                },
                {"longText": "Japan", "types": ["country", "political"]},
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "Taito City"
        assert obj.location.neighborhood == "Asakusa"
        assert obj.location.country == "Japan"
