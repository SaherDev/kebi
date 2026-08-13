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


class TestDeepAdminLevelsAndHumanNames:
    """ADR-163: where no sublocality exists, the deepest admin level that
    names a human area wins the neighborhood slot — and unit words fold out
    of the stored display names."""

    @staticmethod
    def _raw_with_components(components: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": "ChIJdeep",
            "displayName": {"text": "Deep Admin Test"},
            "location": {"latitude": 1.0, "longitude": 2.0},
            "types": [],
            "addressComponents": components,
        }

    def test_bali_shape_level_4_beats_the_regency(self) -> None:
        """Real shape from google:ChIJ... (Old Man's, Canggu): Indonesia has
        no locality; the village (level_4) is the human area, the regency
        (level_2) the failure ADR-153 recorded."""
        raw = self._raw_with_components(
            [
                {"longText": "Jalan Pantai Batu Bolong", "types": ["route"]},
                {
                    "longText": "Canggu",
                    "types": ["administrative_area_level_4", "political"],
                },
                {
                    "longText": "Kecamatan Kuta Utara",
                    "types": ["administrative_area_level_3", "political"],
                },
                {
                    "longText": "Kabupaten Badung",
                    "types": ["administrative_area_level_2", "political"],
                },
                {
                    "longText": "Bali",
                    "types": ["administrative_area_level_1", "political"],
                },
                {
                    "longText": "Indonesia",
                    "shortText": "ID",
                    "types": ["country", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "Bali"
        assert obj.location.neighborhood == "Canggu"

    def test_level_3_falls_back_when_no_level_4(self) -> None:
        raw = self._raw_with_components(
            [
                {
                    "longText": "Kecamatan Kuta Utara",
                    "types": ["administrative_area_level_3", "political"],
                },
                {
                    "longText": "Kabupaten Badung",
                    "types": ["administrative_area_level_2", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        # level_3 wins over level_2, and its unit word ("Kecamatan") folds.
        assert obj.location.neighborhood == "Kuta Utara"

    def test_stored_display_names_are_the_human_names(self) -> None:
        """Bangkok shape: the district's unit word strips from the stored
        neighborhood so screens read "Bang Rak", not "Khet Bang Rak"."""
        raw = self._raw_with_components(
            [
                {
                    "longText": "Khet Bang Rak",
                    "types": ["sublocality_level_1", "political"],
                },
                {
                    "longText": "Krung Thep Maha Nakhon",
                    "types": ["administrative_area_level_1", "political"],
                },
                {
                    "longText": "Thailand",
                    "shortText": "TH",
                    "types": ["country", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.neighborhood == "Bang Rak"
        assert obj.location.city == "Krung Thep Maha Nakhon"

    def test_compound_city_names_survive_the_strip(self) -> None:
        raw = self._raw_with_components(
            [
                {"longText": "Kota Kinabalu", "types": ["locality", "political"]},
                {
                    "longText": "Malaysia",
                    "shortText": "MY",
                    "types": ["country", "political"],
                },
            ]
        )
        obj = map_place(raw, _NOW)

        assert obj is not None and obj.location is not None
        assert obj.location.city == "Kota Kinabalu"
