"""Tests for the knowledge layer's key/slug primitives (ADR-120).

Geo entity keys are the geo registry's since the identity migration — see
`tests/core/geo/` for resolution and minting. What stays here is what this
module still owns: the place key namespace, the transliterating slug that
feeds alias lookups, and slug-equality matching.
"""

from __future__ import annotations

from kebi.core.knowledge.schemas import _slugify, build_place_key, slugs_match


def test_build_place_key_namespaces_the_catalog_id() -> None:
    assert build_place_key("9f3c2a") == "place:9f3c2a"


class TestSlugify:
    def test_diacritics_fold_to_ascii(self) -> None:
        """A diacritic-form name and its ASCII spelling must slug identically,
        or one alias lookup misses what another wrote."""
        assert _slugify("Hội An") == _slugify("Hoi An") == "hoi-an"
        assert _slugify("Hà Giang") == "ha-giang"

    def test_vietnamese_d_stroke_folds_to_d(self) -> None:
        """Đ (U+0110) is a hard case for naive diacritic-stripping; anyascii
        romanises it so "Đà Nẵng" and "Da Nang" collapse to one slug."""
        assert _slugify("Đà Nẵng") == _slugify("Da Nang") == "da-nang"

    def test_non_latin_script_transliterates_to_stable_ascii(self) -> None:
        """Thai / CJK names romanise to a stable ASCII slug rather than
        blanking to an empty string that collides every such name."""
        assert _slugify("กรุงเทพ") == "krungethph"
        assert _slugify("東京") == "dongjing"

    def test_punctuation_and_spacing_collapse(self) -> None:
        assert _slugify("Jumeirah  Beach!") == "jumeirah-beach"


class TestSlugsMatch:
    def test_matches_across_scripts_and_diacritics(self) -> None:
        assert slugs_match("Hội An", "Hoi An")
        assert slugs_match("Đà Nẵng", "Da Nang")

    def test_different_names_do_not_match(self) -> None:
        assert not slugs_match("Canggu", "Ubud")

    def test_missing_sides_never_match(self) -> None:
        assert not slugs_match(None, "Canggu")
        assert not slugs_match("Canggu", "")
