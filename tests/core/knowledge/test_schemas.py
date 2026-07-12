"""Tests for the canonical entity-key builders (ADR-120)."""

from __future__ import annotations

import pytest

from kebi.core.knowledge.schemas import build_geo_key, build_place_key


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
