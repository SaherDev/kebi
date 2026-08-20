"""Tests for AreaHandleBuilder — the one minter of routable area identities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.areas.handles import AreaHandleBuilder
from kebi.core.areas.keys import decode_area_id
from kebi.core.areas.models import AreaProfile
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_BALI = make_city("id", "Bali", pid="CityBali01")
_CANGGU = make_area(_BALI, "Canggu", pid="AreaCanggu01")
_KUTA_UTARA = make_area(_BALI, "Kuta Utara", pid="AreaKutaUtara1")
_CANGGU_KEY = _CANGGU.geo_key
_KUTA_KEY = _KUTA_UTARA.geo_key


def _profile(geo_key: str, name: str, icon: str | None = None) -> AreaProfile:
    return AreaProfile(
        geo_key=geo_key, name=name, level="neighbourhood", icon=icon, summary="…"
    )


def _builder(
    profiles: dict[str, AreaProfile] | None = None,
    registry: FakeGeoRegistry | None = None,
) -> AreaHandleBuilder:
    repo = MagicMock(get_many=AsyncMock(return_value=profiles or {}))
    return AreaHandleBuilder(
        area_repo=repo,
        geo_registry=registry or FakeGeoRegistry(_BALI, _CANGGU, _KUTA_UTARA),
    )


class TestNaming:
    async def test_profiled_area_uses_its_row_name(self) -> None:
        builder = _builder({_CANGGU_KEY: _profile(_CANGGU_KEY, "Canggu Area", "🏄")})

        handles = await builder.for_keys([_CANGGU_KEY])

        assert handles[_CANGGU_KEY].name == "Canggu Area"
        assert handles[_CANGGU_KEY].icon == "🏄"

    async def test_unprofiled_area_is_named_by_its_registry_row(self) -> None:
        """Area rows are written lazily on first screen open, so most keys
        have no profile. The registry row always exists (mint-before-key)
        and supplies the name; the handle stays tappable."""
        handles = await _builder().for_keys([_KUTA_KEY])

        handle = handles[_KUTA_KEY]
        assert handle.name == "Kuta Utara"  # the registry row's name
        assert handle.icon is None  # client falls back
        assert handle.uri.startswith("kebi://area/")

    async def test_colloquial_name_wins_over_the_official_one(self) -> None:
        registry = FakeGeoRegistry(
            _BALI,
            make_area(
                _BALI,
                "Tibubeneng",
                pid="AreaTibubeneng1",
                colloquial_name="Berawa",
            ),
        )
        key = "id/CityBali01/AreaTibubeneng1"

        handles = await _builder(registry=registry).for_keys([key])

        assert handles[key].name == "Berawa"

    async def test_unregistered_country_key_shows_its_code(self) -> None:
        """A bare country key may predate any mint; the profiler's dressing
        replaces this — until then it shows as the code."""
        handles = await _builder(registry=FakeGeoRegistry()).for_keys(["id"])

        assert handles["id"].name == "ID"


class TestUri:
    async def test_uri_round_trips_to_the_key(self) -> None:
        """Composed server-side because the key is slash-hierarchical and has
        to go through a codec — a client rebuilding it would be guessing at an
        encoding it cannot see."""
        handles = await _builder().for_keys([_CANGGU_KEY])

        token = handles[_CANGGU_KEY].uri.removeprefix("kebi://area/")
        assert decode_area_id(token) == _CANGGU_KEY


class TestParent:
    async def test_parent_is_the_area_one_level_up(self) -> None:
        handles = await _builder(
            {_BALI.geo_key: _profile(_BALI.geo_key, "Bali")}
        ).for_keys([_CANGGU_KEY])

        parent = handles[_CANGGU_KEY].parent
        assert parent is not None
        assert (parent.key, parent.name) == (_BALI.geo_key, "Bali")

    async def test_city_level_area_has_no_parent(self) -> None:
        """A country is not an area anyone navigates to, so the chain stops."""
        handles = await _builder().for_keys([_BALI.geo_key])

        assert handles[_BALI.geo_key].parent is None


class TestBatching:
    async def test_one_lookup_covers_every_key_and_its_parents(self) -> None:
        repo = MagicMock(get_many=AsyncMock(return_value={}))
        builder = AreaHandleBuilder(area_repo=repo, geo_registry=FakeGeoRegistry())

        await builder.for_keys(
            [
                "id/CityBali01/AreaCanggu01",
                "id/CityBali01/AreaUbud0001",
                "th/CityBangkok1/AreaThonglor1",
            ]
        )

        repo.get_many.assert_awaited_once()
        asked = set(repo.get_many.await_args.args[0])
        # Leaves plus their parents, deduped — Bali is asked for once.
        assert asked == {
            "id/CityBali01/AreaCanggu01",
            "id/CityBali01/AreaUbud0001",
            "id/CityBali01",
            "th/CityBangkok1/AreaThonglor1",
            "th/CityBangkok1",
        }

    async def test_no_keys_means_no_query(self) -> None:
        repo = MagicMock(get_many=AsyncMock(return_value={}))
        builder = AreaHandleBuilder(area_repo=repo, geo_registry=FakeGeoRegistry())

        assert await builder.for_keys([]) == {}
        repo.get_many.assert_not_awaited()
