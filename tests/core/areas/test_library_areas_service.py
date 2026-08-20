"""Tests for LibraryAreasService — the library's at-rest area index."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.areas.handles import AreaHandleBuilder
from kebi.core.areas.library_areas_service import LibraryAreasService
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_BALI = make_city("id", "Bali", pid="CityBali01")
_CANGGU = make_area(_BALI, "Canggu", pid="AreaCanggu01")
_BANGKOK = make_city("th", "Bangkok", pid="CityBangkok1")
_THONGLOR = make_area(_BANGKOK, "Thonglor", pid="AreaThonglor1")


def _service(distribution: list[tuple[str, int]]) -> LibraryAreasService:
    return LibraryAreasService(
        user_places_repo=MagicMock(
            area_distribution=AsyncMock(return_value=distribution)
        ),
        handle_builder=AreaHandleBuilder(
            area_repo=MagicMock(get_many=AsyncMock(return_value={})),
            geo_registry=FakeGeoRegistry(_BALI, _CANGGU, _BANGKOK, _THONGLOR),
        ),
    )


class TestListAreas:
    async def test_every_area_gets_a_handle_and_its_count(self) -> None:
        svc = _service([(_CANGGU.geo_key, 11), (_THONGLOR.geo_key, 4)])

        areas = await svc.list_areas("u1")

        assert [(a.area.key, a.count) for a in areas] == [
            (_CANGGU.geo_key, 11),
            (_THONGLOR.geo_key, 4),
        ]
        assert areas[0].area.name == "Canggu"
        assert areas[0].area.parent is not None

    async def test_counts_come_from_the_repo_not_a_page(self) -> None:
        """The whole point: a count derived from loaded rows says "4 so far"
        on first paint. This number is the library's, not the page's."""
        svc = _service([(_CANGGU.geo_key, 11)])

        areas = await svc.list_areas("u1")

        assert areas[0].count == 11

    async def test_empty_library_is_an_empty_index(self) -> None:
        svc = _service([])

        assert await svc.list_areas("u1") == []

    async def test_no_area_lookup_when_there_is_nothing_to_look_up(self) -> None:
        repo = MagicMock(get_many=AsyncMock(return_value={}))
        svc = LibraryAreasService(
            user_places_repo=MagicMock(area_distribution=AsyncMock(return_value=[])),
            handle_builder=AreaHandleBuilder(
                area_repo=repo, geo_registry=FakeGeoRegistry()
            ),
        )

        await svc.list_areas("u1")

        repo.get_many.assert_not_awaited()
