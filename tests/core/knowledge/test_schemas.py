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
