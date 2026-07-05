"""Tests for places domain models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kebi.core.places.models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceObject,
    PlaceQuery,
    PlaceSource,
    PlaceTag,
    SavedPlaceView,
    UserPlace,
    normalize_icon,
)


class TestUserPlaceValidation:
    def test_url_required_for_tiktok(self) -> None:
        with pytest.raises(ValidationError, match="source_ref is required"):
            UserPlace(
                user_place_id="up1",
                user_id="u1",
                place_id="p1",
                source=PlaceSource.tiktok,
                source_ref=None,
                saved_at=datetime.now(UTC),
            )

    def test_url_forbidden_for_manual(self) -> None:
        with pytest.raises(ValidationError, match="source_ref must be None"):
            UserPlace(
                user_place_id="up1",
                user_id="u1",
                place_id="p1",
                source=PlaceSource.manual,
                source_ref="https://example.com",
                saved_at=datetime.now(UTC),
            )

    def test_url_forbidden_for_kebi(self) -> None:
        with pytest.raises(ValidationError, match="source_ref must be None"):
            UserPlace(
                user_place_id="up1",
                user_id="u1",
                place_id="p1",
                source=PlaceSource.kebi,
                source_ref="https://example.com",
                saved_at=datetime.now(UTC),
            )

    def test_valid_manual_source(self) -> None:
        up = UserPlace(
            user_place_id="up1",
            user_id="u1",
            place_id="p1",
            source=PlaceSource.manual,
            source_ref=None,
            saved_at=datetime.now(UTC),
        )
        assert up.source == PlaceSource.manual
        assert up.source_ref is None

    def test_valid_tiktok_source(self) -> None:
        up = UserPlace(
            user_place_id="up1",
            user_id="u1",
            place_id="p1",
            source=PlaceSource.tiktok,
            source_ref="https://tiktok.com/v/123",
            saved_at=datetime.now(UTC),
        )
        assert up.source_ref == "https://tiktok.com/v/123"

    def test_defaults(self) -> None:
        up = UserPlace(
            user_place_id="up1",
            user_id="u1",
            place_id="p1",
            source=PlaceSource.manual,
            saved_at=datetime.now(UTC),
        )
        assert up.approved is True
        assert up.visited is False
        assert up.liked is None


class TestPlaceTag:
    def test_construction(self) -> None:
        tag = PlaceTag(type="cuisine", value="Thai", source="google")
        assert tag.type == "cuisine"
        assert tag.value == "Thai"
        assert tag.source == "google"

    def test_price_tag(self) -> None:
        tag = PlaceTag(type="price", value="$$", source="google")
        assert tag.type == "price"
        assert tag.value == "$$"

    def test_manual_tag(self) -> None:
        tag = PlaceTag(type="atmosphere", value="cozy", source="manual")
        assert tag.source == "manual"


class TestPlaceCore:
    def test_defaults(self) -> None:
        core = PlaceCore(place_name="Sukhumvit Joe's")
        assert core.tags == []
        assert core.id is None
        assert core.provider_id is None

    def test_full_construction(self) -> None:
        core = PlaceCore(
            id="abc",
            provider_id="google:ChIJ123",
            place_name="Sukhumvit Joe's",
            categories=["restaurant", "bar"],
            tags=[
                PlaceTag(type="cuisine", value="Thai", source="google"),
                PlaceTag(type="price", value="$$", source="google"),
            ],
            location=LocationContext(
                lat=13.756, lng=100.502, address="1 Sukhumvit, Bangkok"
            ),
        )
        assert core.provider_id == "google:ChIJ123"
        assert core.categories == [PlaceCategory.restaurant, PlaceCategory.bar]
        assert len(core.tags) == 2
        assert core.tags[0].value == "Thai"
        assert core.tags[1].type == "price"


class TestPlaceObject:
    def test_extends_place_core(self) -> None:
        obj = PlaceObject(
            place_name="Test",
            provider_id="google:xyz",
            rating=4.5,
            popularity=1200,
        )
        assert obj.rating == 4.5
        assert obj.place_name == "Test"
        assert obj.tags == []
        assert obj.hours is None
        assert obj.phone is None


class TestPlaceQuery:
    def test_all_optional(self) -> None:
        q = PlaceQuery()
        assert q.categories is None
        assert q.tags is None
        assert q.location is None

    def test_location_context_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            LocationContext(city="Bangkok", unknown_field="x")  # type: ignore[call-arg]

    def test_distance_sort_requires_location(self) -> None:
        with pytest.raises(ValidationError, match="sort_by='distance' requires"):
            PlaceQuery(sort_by="distance")

    def test_distance_sort_requires_coords_not_named_area(self) -> None:
        with pytest.raises(ValidationError, match="sort_by='distance' requires"):
            PlaceQuery(sort_by="distance", location=LocationContext(city="Bangkok"))

    def test_distance_sort_with_coords_ok(self) -> None:
        q = PlaceQuery(
            sort_by="distance",
            location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
        )
        assert q.sort_by == "distance"

    def test_non_distance_sort_needs_no_location(self) -> None:
        for field in ("created_at", "refreshed_at", "place_name"):
            q = PlaceQuery(sort_by=field)  # type: ignore[arg-type]
            assert q.sort_by == field
            assert q.location is None


class TestNormalizeIcon:
    def test_keeps_single_emoji(self) -> None:
        assert normalize_icon("🌴") == "🌴"

    def test_keeps_multi_codepoint_emoji(self) -> None:
        # Variation selector (🏖️) and flag pairs are > 1 codepoint.
        assert normalize_icon("🏖️") == "🏖️"
        assert normalize_icon("🇹🇭") == "🇹🇭"

    def test_strips_whitespace(self) -> None:
        assert normalize_icon(" ⛲ ") == "⛲"

    def test_rejects_none_and_empty(self) -> None:
        assert normalize_icon(None) is None
        assert normalize_icon("") is None
        assert normalize_icon("   ") is None

    def test_rejects_ascii_words(self) -> None:
        assert normalize_icon("palm") is None
        assert normalize_icon("a🌴") is None

    def test_rejects_overlong_sequences(self) -> None:
        assert normalize_icon("🌴🌴🌴🌴🌴🌴🌴🌴🌴") is None


class TestPlaceCoreIcon:
    def test_defaults_to_none(self) -> None:
        assert PlaceCore(place_name="Cafe X").icon is None

    def test_validator_normalizes_junk_to_none(self) -> None:
        assert PlaceCore(place_name="Cafe X", icon="not an emoji").icon is None

    def test_validator_keeps_valid_icon(self) -> None:
        assert PlaceCore(place_name="Cafe X", icon="🗼").icon == "🗼"

    def test_to_core_carries_icon(self) -> None:
        obj = PlaceObject(place_name="Cafe X", icon="🍜")
        assert obj.to_core().icon == "🍜"


class TestSavedPlaceView:
    def test_construction(self) -> None:
        place = PlaceCore(place_name="Cafe X")
        up = UserPlace(
            user_place_id="u1",
            user_id="user",
            place_id="p1",
            source=PlaceSource.kebi,
            saved_at=datetime.now(UTC),
        )
        view = SavedPlaceView(place=place, user_data=up)
        assert view.place.place_name == "Cafe X"


class TestPlaceQueryIconHint:
    def test_defaults_to_none(self) -> None:
        assert PlaceQuery().icon_hint is None

    def test_junk_hint_normalized_to_none(self) -> None:
        assert PlaceQuery(icon_hint="palm tree").icon_hint is None

    def test_valid_hint_kept(self) -> None:
        assert PlaceQuery(icon_hint="🌴").icon_hint == "🌴"
