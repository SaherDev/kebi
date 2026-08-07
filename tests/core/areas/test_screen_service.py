"""Tests for the area screen composition (ADR-153).

The service's contract: saves are scoped by the same key the claims use;
a leaf shows venue rows, a wide level groups saves into child-area rows
(with direct-level saves as venue rows so `saved_count` never counts hidden
rows); no saves falls back to the profile's notable children; profiled
children lend their name/icon/hook to the rows; breadcrumbs prefer real
names over slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kebi.core.areas.models import AreaChip, AreaProfile, NotableSubArea
from kebi.core.areas.screen_service import AreaScreenService
from kebi.core.places.models import (
    LocationContext,
    PlaceCore,
    PlaceSource,
    PlaceTag,
    UserPlace,
)

_USER = "user_test_dummy_123456789012345"


class _FakeAreaRepo:
    def __init__(self, profiles: dict[str, AreaProfile] | None = None) -> None:
        self.profiles = profiles or {}

    async def get(self, geo_key: str) -> AreaProfile | None:
        return self.profiles.get(geo_key)

    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]:
        return {k: self.profiles[k] for k in geo_keys if k in self.profiles}

    async def upsert(self, profile: AreaProfile) -> AreaProfile:
        self.profiles[profile.geo_key] = profile
        return profile


class _FakeUserPlacesRepo:
    def __init__(self, saves: list[UserPlace]) -> None:
        self.saves = saves

    async def get_by_user(self, user_id: str) -> list[UserPlace]:
        return self.saves


class _FakePlacesRepo:
    def __init__(self, cores: list[PlaceCore]) -> None:
        self.cores = {c.id: c for c in cores}

    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]:
        return [self.cores[p] for p in place_ids if p in self.cores]


def _core(
    place_id: str,
    name: str,
    *,
    city: str = "Bali",
    neighborhood: str | None = "Canggu",
    country_code: str | None = "id",
    tags: list[PlaceTag] | None = None,
    icon: str | None = None,
    categories: list[str] | None = None,
) -> PlaceCore:
    return PlaceCore(
        id=place_id,
        place_name=name,
        icon=icon,
        categories=categories or [],  # type: ignore[arg-type]
        tags=tags or [],
        location=LocationContext(
            city=city,
            neighborhood=neighborhood,
            country="Indonesia",
            country_code=country_code,
        ),
    )


def _save(place_id: str, *, liked: bool | None = None, day: int = 1) -> UserPlace:
    return UserPlace(
        user_place_id=f"up-{place_id}",
        user_id=_USER,
        place_id=place_id,
        source=PlaceSource.manual,
        liked=liked,
        saved_at=datetime(2026, 8, day, tzinfo=UTC),
    )


def _profile(geo_key: str, **overrides: Any) -> AreaProfile:
    base: dict[str, Any] = {
        "geo_key": geo_key,
        "name": "Canggu",
        "level": "neighbourhood",
        "icon": "🏄",
        "summary": "the surf-and-laptop end of bali.",
        "best_for": [
            AreaChip(icon="🌅", text="sunset drinks"),
            AreaChip(icon="💻", text="café work"),
        ],
        "breadcrumb": ["Indonesia", "Bali"],
    }
    base.update(overrides)
    return AreaProfile(**base)


def _service(
    profiles: dict[str, AreaProfile] | None = None,
    saves: list[UserPlace] | None = None,
    cores: list[PlaceCore] | None = None,
) -> AreaScreenService:
    return AreaScreenService(
        area_repo=_FakeAreaRepo(profiles),
        user_places_repo=_FakeUserPlacesRepo(saves or []),  # type: ignore[arg-type]
        places_repo=_FakePlacesRepo(cores or []),  # type: ignore[arg-type]
    )


async def test_leaf_with_saves_shows_venue_rows_newest_first() -> None:
    svc = _service(
        profiles={"id/bali/canggu": _profile("id/bali/canggu")},
        saves=[_save("p1", day=1), _save("p2", liked=True, day=2)],
        cores=[
            _core("p1", "Crate Café", categories=["cafe"]),
            _core("p2", "Savaya Bali"),
        ],
    )

    screen = await svc.build_screen("id/bali/canggu", _USER)

    assert screen.section_kind == "saved"
    assert screen.saved_count == 2
    assert [v.place_id for v in screen.venues] == ["p2", "p1"]
    assert screen.venues[0].liked is True
    assert screen.venues[1].subtitle == "cafe"
    assert screen.sub_areas == []


async def test_wide_level_groups_saves_into_child_area_rows() -> None:
    svc = _service(
        saves=[_save("p1"), _save("p2"), _save("p3")],
        cores=[
            _core("p1", "Crate Café", neighborhood="Canggu"),
            _core("p2", "Savaya Bali", neighborhood="Canggu"),
            _core("p3", "Zest", neighborhood="Ubud"),
        ],
    )

    screen = await svc.build_screen("id/bali", _USER)

    assert screen.section_kind == "saved"
    assert screen.saved_count == 3
    assert [(a.geo_key, a.saved_count) for a in screen.sub_areas] == [
        ("id/bali/canggu", 2),
        ("id/bali/ubud", 1),
    ]
    assert screen.venues == []


async def test_a_save_with_no_deeper_geo_appears_as_a_venue_row() -> None:
    # `saved_count` counts it, so the screen must show it somewhere.
    svc = _service(
        saves=[_save("p1"), _save("p2")],
        cores=[
            _core("p1", "Crate Café", neighborhood="Canggu"),
            _core("p2", "Warung Pantai", neighborhood=None),
        ],
    )

    screen = await svc.build_screen("id/bali", _USER)

    assert screen.saved_count == 2
    assert [a.geo_key for a in screen.sub_areas] == ["id/bali/canggu"]
    assert [v.place_id for v in screen.venues] == ["p2"]


async def test_a_profiled_child_lends_name_icon_and_hook_to_its_row() -> None:
    svc = _service(
        profiles={"id/bali/canggu": _profile("id/bali/canggu")},
        saves=[_save("p1")],
        cores=[_core("p1", "Crate Café", neighborhood="Canggu")],
    )

    screen = await svc.build_screen("id/bali", _USER)

    row = screen.sub_areas[0]
    assert row.name == "Canggu"
    assert row.icon == "🏄"
    assert row.hook == "sunset drinks · café work"


async def test_an_unprofiled_child_names_from_geo_and_hooks_from_tags() -> None:
    svc = _service(
        saves=[_save("p1")],
        cores=[
            _core(
                "p1",
                "Crate Café",
                neighborhood="Canggu",
                tags=[
                    PlaceTag(type="atmosphere", value="lively", source="llm"),
                    PlaceTag(type="feature", value="fast_wifi", source="llm"),
                ],
            )
        ],
    )

    screen = await svc.build_screen("id/bali", _USER)

    row = screen.sub_areas[0]
    assert row.name == "Canggu"
    assert row.icon is None
    assert row.hook == "lively · fast wifi"


async def test_no_saves_falls_back_to_the_profiles_notable_children() -> None:
    svc = _service(
        profiles={
            "id/bali": _profile(
                "id/bali",
                name="Bali",
                level="region",
                breadcrumb=["Indonesia"],
                notable_sub_areas=[
                    NotableSubArea(
                        geo_key="id/bali/canggu",
                        name="Canggu",
                        icon="🏄",
                        hook="surf & laptops",
                    )
                ],
            )
        },
    )

    screen = await svc.build_screen("id/bali", _USER)

    assert screen.section_kind == "worth_knowing"
    assert screen.saved_count == 0
    assert [a.geo_key for a in screen.sub_areas] == ["id/bali/canggu"]
    assert screen.sub_areas[0].hook == "surf & laptops"


async def test_no_saves_and_no_profile_shows_a_thin_screen_with_no_section() -> None:
    screen = await _service().build_screen("id/bali/canggu", _USER)

    assert screen.profiled is False
    assert screen.section_kind is None
    assert screen.name == "Canggu"  # slug-derived fallback
    assert screen.summary is None
    assert [b.geo_key for b in screen.breadcrumb] == ["id", "id/bali"]


async def test_breadcrumb_prefers_recorded_names_over_slugs() -> None:
    svc = _service(profiles={"id/bali/canggu": _profile("id/bali/canggu")})

    screen = await svc.build_screen("id/bali/canggu", _USER)

    assert [(b.geo_key, b.name) for b in screen.breadcrumb] == [
        ("id", "Indonesia"),
        ("id/bali", "Bali"),
    ]


async def test_breadcrumb_prefers_an_ancestors_own_row_name() -> None:
    svc = _service(
        profiles={
            "id/bali/canggu": _profile("id/bali/canggu", breadcrumb=["", "bali??"]),
            "id/bali": _profile(
                "id/bali", name="Bali", level="region", breadcrumb=["Indonesia"]
            ),
        },
    )

    screen = await svc.build_screen("id/bali/canggu", _USER)

    assert screen.breadcrumb[1].name == "Bali"


async def test_saves_outside_the_key_are_invisible() -> None:
    svc = _service(
        saves=[_save("p1"), _save("p2")],
        cores=[
            _core("p1", "Crate Café"),
            _core("p2", "Bar Trigona", city="Kuala Lumpur", country_code="my"),
        ],
    )

    screen = await svc.build_screen("id/bali/canggu", _USER)

    assert screen.saved_count == 1
    assert [v.place_id for v in screen.venues] == ["p1"]


async def test_a_save_whose_place_lacks_geo_is_skipped_not_guessed() -> None:
    svc = _service(
        saves=[_save("p1")],
        cores=[_core("p1", "Mystery Spot", country_code=None)],
    )

    screen = await svc.build_screen("id/bali/canggu", _USER)

    assert screen.saved_count == 0
    assert screen.section_kind is None
