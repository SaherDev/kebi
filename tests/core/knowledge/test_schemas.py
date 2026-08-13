"""Tests for the canonical entity-key builders (ADR-120)."""

from __future__ import annotations

import pytest

from kebi.core.knowledge.schemas import (
    _CITY_ALIASES,
    build_geo_key,
    build_place_key,
    canonical_area_slug,
    canonical_city_slug,
    strip_admin_unit,
)


def test_build_place_key_namespaces_the_catalog_id() -> None:
    assert build_place_key("9f3c2a") == "place:9f3c2a"


def test_build_geo_key_country_only() -> None:
    assert build_geo_key("AE") == "ae"


def test_build_geo_key_city() -> None:
    assert build_geo_key("ae", "Dubai") == "ae/dubai"


def test_build_geo_key_neighborhood() -> None:
    assert build_geo_key("ae", "Dubai", "Jumeirah Beach") == "ae/dubai/jumeirah-beach"


def test_build_geo_key_rejects_bad_country_code() -> None:
    with pytest.raises(ValueError, match="ISO-3166"):
        build_geo_key("United Arab Emirates")


def test_build_geo_key_rejects_neighborhood_without_city() -> None:
    with pytest.raises(ValueError, match="requires a city"):
        build_geo_key("ae", neighborhood="Jumeirah")


def test_two_different_named_springfields_do_not_collide() -> None:
    assert build_geo_key("us", "Springfield") != build_geo_key("au", "Springfield")


def test_diacritics_fold_to_ascii_so_one_city_keys_once() -> None:
    """A diacritic-form name and its ASCII spelling must key identically, or
    the same city's claims fragment across two entity_keys."""
    assert build_geo_key("vn", "Hội An") == build_geo_key("vn", "Hoi An") == "vn/hoi-an"
    assert build_geo_key("vn", "Hà Giang") == "vn/ha-giang"


def test_vietnamese_d_stroke_folds_to_d() -> None:
    """Đ (U+0110) is a hard case for naive diacritic-stripping; anyascii
    romanises it so "Đà Nẵng" and "Da Nang" collapse to one key."""
    assert build_geo_key("vn", "Đà Nẵng") == "vn/da-nang"
    assert build_geo_key("vn", "Da Nang") == "vn/da-nang"


def test_non_latin_script_transliterates_to_stable_ascii() -> None:
    """Thai / CJK names romanise to a stable ASCII slug (anyascii), so a name
    in its local script and its romanised spelling collapse to one key rather
    than blanking to an empty slug that collides every such name."""
    assert build_geo_key("th", "กรุงเทพ") == "th/krungethph"
    assert build_geo_key("jp", "東京") == "jp/dongjing"


def test_slug_collapses_punctuation_and_spacing() -> None:
    assert build_geo_key("ae", "Dubai", "Jumeirah  Beach!") == "ae/dubai/jumeirah-beach"


class TestCanonicalCitySlug:
    """One city, one key (ADR-144).

    The geocoder returns an English exonym on one lookup and the local
    endonym on the next, and `_slugify` cannot merge those — they are
    different words, not transliterations. Left alone, a city's claims split
    across two keys that no prefix scan will ever join.
    """

    def test_the_local_name_folds_to_the_canonical_slug(self) -> None:
        assert build_geo_key("th", "Krung Thep Maha Nakhon") == "th/bangkok"
        assert build_geo_key("th", "Bangkok") == "th/bangkok"

    def test_a_neighborhood_inherits_the_canonical_parent(self) -> None:
        # This is the case that actually broke: the neighborhood claim sat
        # under a different city key than the city's own claims. The unit
        # word folds too (ADR-163), so "Khet Khlong Toei" and "Khlong Toei"
        # cannot split the district.
        assert (
            build_geo_key("th", "Krung Thep Maha Nakhon", "Khet Khlong Toei")
            == "th/bangkok/khlong-toei"
        )

    def test_an_unknown_city_passes_through_unchanged(self) -> None:
        assert build_geo_key("vn", "Hoi An") == "vn/hoi-an"

    def test_transliteration_still_collapses_without_an_alias(self) -> None:
        # anyascii already handles script variants; aliases are only for
        # genuinely different names.
        assert build_geo_key("vn", "Đà Nẵng") == build_geo_key("vn", "Da Nang")

    def test_aliases_are_scoped_by_country(self) -> None:
        # A slug that is an alias in one country must not rewrite another's.
        assert canonical_city_slug("th", "krung thep maha nakhon") == "bangkok"
        assert canonical_city_slug("xx", "krung thep maha nakhon") == (
            "krung-thep-maha-nakhon"
        )

    def test_every_alias_target_is_itself_canonical(self) -> None:
        """No alias may point at another alias, or the fold is order-dependent
        and one hop lands on a key that folds again."""
        for (country_code, _variant), canonical in _CITY_ALIASES.items():
            assert canonical_city_slug(country_code, canonical) == canonical


class TestAdminUnitFolding:
    """Admin-unit affixes fold out of area names and slugs (ADR-163), so an
    official municipality style and the human name key — and read — the same."""

    def test_leading_unit_word_folds_in_the_slug(self) -> None:
        assert canonical_area_slug("khet-khlong-toei") == "khlong-toei"
        assert canonical_area_slug("kabupaten-badung") == "badung"
        assert canonical_area_slug("thanh-pho-hue") == "hue"

    def test_trailing_unit_word_folds_only_where_it_is_a_translation(self) -> None:
        assert canonical_area_slug("ubud-district", "id") == "ubud"
        assert canonical_area_slug("badung-regency", "id") == "badung"
        # In English-speaking countries "District" IS the colloquial name.
        assert canonical_area_slug("financial-district", "us") == "financial-district"
        assert canonical_area_slug("ubud-district") == "ubud-district"

    def test_a_bare_unit_word_never_empties_the_slug(self) -> None:
        assert canonical_area_slug("khet", "th") == "khet"
        assert canonical_area_slug("district", "id") == "district"

    def test_display_name_strips_but_keeps_original_spelling(self) -> None:
        assert strip_admin_unit("Khet Bang Rak", "th") == "Bang Rak"
        assert strip_admin_unit("Thành phố Huế", "vn") == "Huế"
        assert strip_admin_unit("Kec. Kuta Utara", "id") == "Kuta Utara"
        assert strip_admin_unit("Ko Samui District", "th") == "Ko Samui"

    def test_compound_proper_names_are_never_stripped(self) -> None:
        # "Kota"/"City" are parts of real names — the ADR-160 compound trap;
        # a US "District" neighborhood keeps its suffix the same way.
        assert strip_admin_unit("Kota Kinabalu", "my") == "Kota Kinabalu"
        assert strip_admin_unit("Ho Chi Minh City", "vn") == "Ho Chi Minh City"
        assert strip_admin_unit("Financial District", "us") == "Financial District"
        assert strip_admin_unit("Canggu", "id") == "Canggu"

    def test_neighborhood_key_folds_the_unit_word(self) -> None:
        assert build_geo_key("id", "Bali", "Kabupaten Badung") == "id/bali/badung"
        assert (
            build_geo_key("th", "Bangkok", "Khet Bang Rak")
            == build_geo_key("th", "Krung Thep Maha Nakhon", "Bang Rak")
            == "th/bangkok/bang-rak"
        )

    def test_city_key_folds_the_municipality_style(self) -> None:
        assert build_geo_key("vn", "Thành phố Huế") == build_geo_key("vn", "Hue")

    def test_alias_lookup_on_the_raw_slug_wins_over_folding(self) -> None:
        # "Thành phố Hồ Chí Minh" has an explicit alias whose target would
        # not survive naive folding — the raw-slug alias must win.
        assert canonical_city_slug("vn", "Thành phố Hồ Chí Minh") == "ho-chi-minh-city"

    def test_village_level_aliases_fold_to_the_colloquial_area(self) -> None:
        # Google's village admin splits colloquial areas; the alias table
        # folds them like _CITY_ALIASES folds exonyms (ADR-144/163).
        assert build_geo_key("id", "Bali", "Tibubeneng") == "id/bali/canggu"
        assert build_geo_key("id", "Bali", "Pecatu") == "id/bali/uluwatu"
        # Alias scope is the country — no cross-country rewrites.
        assert build_geo_key("vn", "Hue", "Pecatu") == "vn/hue/pecatu"
