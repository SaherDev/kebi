"""Tests for display-only admin-dress cleanup (never identity)."""

from __future__ import annotations

from kebi.core.geo.display import strip_admin_unit


class TestStripAdminUnit:
    def test_display_name_strips_but_keeps_original_spelling(self) -> None:
        assert strip_admin_unit("Khet Bang Rak", "th") == "Bang Rak"
        assert strip_admin_unit("Thành phố Huế", "vn") == "Huế"
        assert strip_admin_unit("Kec. Kuta Utara", "id") == "Kuta Utara"
        assert strip_admin_unit("Ko Samui District", "th") == "Ko Samui"

    def test_trailing_unit_word_strips_only_where_it_is_a_translation(self) -> None:
        assert strip_admin_unit("Ubud District", "id") == "Ubud"
        # In English-speaking countries "District" IS the colloquial name.
        assert strip_admin_unit("Financial District", "us") == "Financial District"
        assert strip_admin_unit("Ubud District") == "Ubud District"

    def test_compound_proper_names_are_never_stripped(self) -> None:
        # "Kota"/"City" are parts of real names — the ADR-160 compound trap.
        assert strip_admin_unit("Kota Kinabalu", "my") == "Kota Kinabalu"
        assert strip_admin_unit("Ho Chi Minh City", "vn") == "Ho Chi Minh City"
        assert strip_admin_unit("Canggu", "id") == "Canggu"

    def test_a_bare_unit_word_never_empties_the_name(self) -> None:
        assert strip_admin_unit("Khet", "th") == "Khet"
