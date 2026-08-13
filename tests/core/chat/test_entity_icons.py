"""Tests for EntityIconRefresher (ADR-162).

A chat chip's icon must be exactly what the screen its tap opens shows,
so every linked entity's icon is re-read from its stored row at attach
time — the row is the only icon picker.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from kebi.core.agent.entity_links import ChatEntity, venue_uri, web_uri
from kebi.core.areas.keys import encode_area_id
from kebi.core.areas.models import AreaProfile
from kebi.core.chat.entity_icons import EntityIconRefresher
from kebi.core.places.models import PlaceCore


def _venue_entity(place_id: str, name: str, icon: str | None = None) -> ChatEntity:
    return ChatEntity(
        kind="venue", key=place_id, name=name, uri=venue_uri(place_id), icon=icon
    )


def _area_entity(geo_key: str, name: str, icon: str | None = None) -> ChatEntity:
    return ChatEntity(
        kind="area",
        key=geo_key,
        name=name,
        uri=f"kebi://area/{encode_area_id(geo_key)}",
        icon=icon,
    )


def _profile(geo_key: str, name: str, icon: str | None) -> AreaProfile:
    return AreaProfile(
        geo_key=geo_key, name=name, level="city", icon=icon, summary="a place"
    )


class _FakeAreaRepo:
    def __init__(self, profiles: dict[str, AreaProfile]) -> None:
        self.profiles = profiles
        self.calls: list[list[str]] = []

    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]:
        self.calls.append(geo_keys)
        return {k: self.profiles[k] for k in geo_keys if k in self.profiles}


class _FakePlacesRepo:
    def __init__(self, rows: list[PlaceCore]) -> None:
        self._rows = {r.id: r for r in rows}
        self.calls: list[list[str]] = []

    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]:
        self.calls.append(place_ids)
        return [self._rows[i] for i in place_ids if i in self._rows]


class _FailingAreaRepo:
    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]:
        raise RuntimeError("db down")


@asynccontextmanager
async def _fake_session() -> Any:
    yield object()


def _refresher(
    places: _FakePlacesRepo | None = None,
    areas: _FakeAreaRepo | None = None,
) -> EntityIconRefresher:
    places = places if places is not None else _FakePlacesRepo([])
    return EntityIconRefresher(
        session_factory=lambda: _fake_session(),  # type: ignore[arg-type, return-value]
        area_repo=areas if areas is not None else _FakeAreaRepo({}),
        places_repo_factory=lambda _session: places,  # type: ignore[arg-type, return-value]
    )


class TestVenueIcons:
    async def test_row_icon_replaces_the_payload_snapshot(self) -> None:
        # The payload snapshot predates the row learning its icon (cold-path
        # persist, place-screen first-open profile) — the row wins.
        places = _FakePlacesRepo(
            [PlaceCore(id="p1", place_name="Sunset Club", icon="🍷")]
        )
        out = await _refresher(places=places).refresh(
            [_venue_entity("p1", "Sunset Club", icon=None)]
        )
        assert out[0].icon == "🍷"

    async def test_row_without_an_icon_ships_none(self) -> None:
        places = _FakePlacesRepo([PlaceCore(id="p1", place_name="Sunset Club")])
        out = await _refresher(places=places).refresh(
            [_venue_entity("p1", "Sunset Club", icon="🍕")]
        )
        assert out[0].icon is None

    async def test_missing_row_keeps_the_payload_snapshot(self) -> None:
        out = await _refresher().refresh(
            [_venue_entity("p1", "Sunset Club", icon="🍕")]
        )
        assert out[0].icon == "🍕"


class TestAreaIcons:
    async def test_profiled_area_carries_the_row_icon(self) -> None:
        areas = _FakeAreaRepo({"id/bali": _profile("id/bali", "Bali", "🏝️")})
        out = await _refresher(areas=areas).refresh([_area_entity("id/bali", "Bali")])
        assert out[0].icon == "🏝️"

    async def test_unprofiled_area_ships_none(self) -> None:
        # No row yet: the screen will pick its own icon on first open, so
        # promising one now is exactly the mismatch this exists to remove.
        out = await _refresher().refresh([_area_entity("id/bali", "Bali", icon="🏄")])
        assert out[0].icon is None


class TestBoundaries:
    async def test_web_entities_are_untouched_and_fetch_nothing(self) -> None:
        places = _FakePlacesRepo([])
        areas = _FakeAreaRepo({})
        entity = ChatEntity(
            kind="web",
            key="https://fifa.com/schedule",
            name="fifa.com",
            uri=web_uri("https://fifa.com/schedule"),
            icon="🌐",
        )
        out = await _refresher(places=places, areas=areas).refresh([entity])
        assert out == [entity]
        assert places.calls == []
        assert areas.calls == []

    async def test_failure_ships_entities_unchanged(self) -> None:
        # Icons are decoration — a dead DB must never cost the answer.
        refresher = EntityIconRefresher(
            session_factory=lambda: _fake_session(),  # type: ignore[arg-type, return-value]
            area_repo=_FailingAreaRepo(),
            places_repo_factory=lambda _session: _FakePlacesRepo([]),  # type: ignore[arg-type, return-value]
        )
        entities = [
            _venue_entity("p1", "Sunset Club", icon="🍕"),
            _area_entity("id/bali", "Bali", icon="🏄"),
        ]
        assert await refresher.refresh(entities) == entities

    async def test_mixed_kinds_resolve_in_one_pass(self) -> None:
        places = _FakePlacesRepo(
            [PlaceCore(id="p1", place_name="Sunset Club", icon="🍷")]
        )
        areas = _FakeAreaRepo({"id/bali": _profile("id/bali", "Bali", "🏝️")})
        out = await _refresher(places=places, areas=areas).refresh(
            [
                _venue_entity("p1", "Sunset Club"),
                _area_entity("id/bali", "Bali"),
                _area_entity("id/bali/canggu", "Canggu", icon="🏄"),
            ]
        )
        assert [e.icon for e in out] == ["🍷", "🏝️", None]
        # One batch read per kind, only for the linked entities.
        assert places.calls == [["p1"]]
        assert areas.calls == [["id/bali", "id/bali/canggu"]]
