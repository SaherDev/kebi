"""Tests for the pure merge policy in core/places_v2/_place_merge.py."""

from __future__ import annotations

from datetime import UTC, datetime

from totoro_ai.core.places_v2._place_merge import merge_place
from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceTag,
)


def _core(**overrides: object) -> PlaceCore:
    defaults: dict[str, object] = {
        "id": "uuid-A",
        "provider_id": "google:abc",
        "place_name": "Cafe Centro",
    }
    return PlaceCore(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestMergePlace:
    def test_first_write_returns_candidate_unchanged(self) -> None:
        candidate = _core(place_name="New Place")
        merged = merge_place(None, candidate)
        assert merged is candidate

    def test_place_name_is_sticky(self) -> None:
        existing = _core(place_name="Cafe Centro")
        candidate = _core(place_name="Café Centro")
        merged = merge_place(existing, candidate)
        assert merged.place_name == "Cafe Centro"

    def test_category_is_sticky(self) -> None:
        existing = _core(category=PlaceCategory.cafe)
        candidate = _core(category=PlaceCategory.restaurant)
        merged = merge_place(existing, candidate)
        assert merged.category == PlaceCategory.cafe

    def test_category_set_when_existing_missing(self) -> None:
        existing = _core(category=None)
        candidate = _core(category=PlaceCategory.restaurant)
        merged = merge_place(existing, candidate)
        assert merged.category == PlaceCategory.restaurant

    def test_location_is_sticky_whole_blob(self) -> None:
        existing_loc = LocationContext(lat=1.0, lng=2.0, neighborhood="Mission")
        candidate_loc = LocationContext(lat=1.0, lng=2.0)
        merged = merge_place(
            _core(location=existing_loc), _core(location=candidate_loc)
        )
        assert merged.location == existing_loc

    def test_location_set_when_existing_missing(self) -> None:
        loc = LocationContext(lat=1.0, lng=2.0, neighborhood="Mission")
        merged = merge_place(_core(location=None), _core(location=loc))
        assert merged.location == loc

    def test_tags_dedup_by_value_existing_wins(self) -> None:
        existing = _core(
            tags=[
                PlaceTag(type="vibe", value="chill", source="google"),
                PlaceTag(type="feature", value="outdoor", source="tiktok"),
            ]
        )
        candidate = _core(
            tags=[
                PlaceTag(type="vibe", value="chill", source="llm"),  # dup → drop
                PlaceTag(type="cuisine", value="italian", source="user"),  # new → add
            ]
        )
        merged = merge_place(existing, candidate)
        values = [(t.value, t.source) for t in merged.tags]
        assert values == [
            ("chill", "google"),     # existing kept (llm dropped)
            ("outdoor", "tiktok"),   # carried over
            ("italian", "user"),     # new appended
        ]

    def test_tags_dedup_collapses_repeats_within_incoming(self) -> None:
        existing = _core(tags=[])
        candidate = _core(
            tags=[
                PlaceTag(type="vibe", value="chill", source="tiktok"),
                PlaceTag(type="feature", value="chill", source="llm"),  # same value
            ]
        )
        merged = merge_place(existing, candidate)
        assert len(merged.tags) == 1
        assert merged.tags[0].source == "tiktok"

    def test_aliases_dedup_by_value_existing_wins(self) -> None:
        existing = _core(
            place_name_aliases=[
                PlaceNameAlias(value="el centro", source="user"),
            ]
        )
        candidate = _core(
            place_name_aliases=[
                PlaceNameAlias(value="el centro", source="llm"),  # dup → drop
                PlaceNameAlias(value="Cafe Centro Mission", source="tiktok"),  # new
            ]
        )
        merged = merge_place(existing, candidate)
        values = [(a.value, a.source) for a in merged.place_name_aliases]
        assert values == [
            ("el centro", "user"),
            ("Cafe Centro Mission", "tiktok"),
        ]

    def test_id_and_provider_id_existing_wins(self) -> None:
        existing = _core(id="uuid-A", provider_id="google:abc")
        candidate = _core(id="uuid-DIFFERENT", provider_id="google:xyz")
        merged = merge_place(existing, candidate)
        assert merged.id == "uuid-A"
        assert merged.provider_id == "google:abc"

    def test_refreshed_at_bumped_on_cold_to_warm(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 4, 1, tzinfo=UTC)
        existing = _core(location=None, refreshed_at=None)
        candidate = _core(
            location=LocationContext(lat=1.0, lng=2.0),
            refreshed_at=new,
        )
        merged = merge_place(existing, candidate)
        assert merged.refreshed_at == new
        # Sanity: irrelevant value to confirm test isn't accidentally matching.
        assert merged.refreshed_at != old

    def test_refreshed_at_preserved_when_existing_already_warm(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 4, 1, tzinfo=UTC)
        existing = _core(
            location=LocationContext(lat=1.0, lng=2.0),
            refreshed_at=old,
        )
        candidate = _core(
            location=LocationContext(lat=1.0, lng=2.0),
            refreshed_at=new,
        )
        merged = merge_place(existing, candidate)
        assert merged.refreshed_at == old

    def test_candidate_with_null_tags_does_not_clobber_existing(self) -> None:
        existing = _core(
            tags=[PlaceTag(type="vibe", value="chill", source="google")]
        )
        candidate = _core(tags=[])
        merged = merge_place(existing, candidate)
        assert len(merged.tags) == 1
        assert merged.tags[0].value == "chill"
