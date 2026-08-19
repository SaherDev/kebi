"""Tests for AreaHandleBuilder — the one minter of routable area identities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from kebi.core.areas.handles import AreaHandleBuilder
from kebi.core.areas.keys import decode_area_id
from kebi.core.areas.models import AreaProfile


def _profile(geo_key: str, name: str, icon: str | None = None) -> AreaProfile:
    return AreaProfile(
        geo_key=geo_key, name=name, level="neighbourhood", icon=icon, summary="…"
    )


def _builder(profiles: dict[str, AreaProfile] | None = None) -> AreaHandleBuilder:
    repo = MagicMock(get_many=AsyncMock(return_value=profiles or {}))
    return AreaHandleBuilder(area_repo=repo)


class TestNaming:
    async def test_profiled_area_uses_its_row_name(self) -> None:
        builder = _builder(
            {"id/bali/canggu": _profile("id/bali/canggu", "Canggu", "🏄")}
        )

        handles = await builder.for_keys(["id/bali/canggu"])

        assert handles["id/bali/canggu"].name == "Canggu"
        assert handles["id/bali/canggu"].icon == "🏄"

    async def test_unprofiled_area_still_gets_a_working_handle(self) -> None:
        """Area rows are written lazily on first screen open, so most keys have
        no profile. A heading for one must still be tappable — its screen
        renders unprofiled too."""
        handles = await _builder().for_keys(["id/bali/kuta-utara"])

        handle = handles["id/bali/kuta-utara"]
        assert handle.name == "Kuta Utara"  # derived from the slug
        assert handle.icon is None  # client falls back
        assert handle.uri.startswith("kebi://area/")


class TestUri:
    async def test_uri_round_trips_to_the_key(self) -> None:
        """Composed server-side because the key is slash-hierarchical and has
        to go through a codec — a client rebuilding it would be guessing at an
        encoding it cannot see."""
        handles = await _builder().for_keys(["id/bali/canggu"])

        token = handles["id/bali/canggu"].uri.removeprefix("kebi://area/")
        assert decode_area_id(token) == "id/bali/canggu"


class TestParent:
    async def test_parent_is_the_area_one_level_up(self) -> None:
        handles = await _builder({"id/bali": _profile("id/bali", "Bali")}).for_keys(
            ["id/bali/canggu"]
        )

        parent = handles["id/bali/canggu"].parent
        assert parent is not None
        assert (parent.key, parent.name) == ("id/bali", "Bali")

    async def test_city_level_area_has_no_parent(self) -> None:
        """A country is not an area anyone navigates to, so the chain stops."""
        handles = await _builder().for_keys(["th/bangkok"])

        assert handles["th/bangkok"].parent is None


class TestBatching:
    async def test_one_lookup_covers_every_key_and_its_parents(self) -> None:
        repo = MagicMock(get_many=AsyncMock(return_value={}))
        builder = AreaHandleBuilder(area_repo=repo)

        await builder.for_keys(
            ["id/bali/canggu", "id/bali/ubud", "th/bangkok/thonglor"]
        )

        repo.get_many.assert_awaited_once()
        asked = set(repo.get_many.await_args.args[0])
        # Leaves plus their parents, deduped — Bali is asked for once.
        assert asked == {
            "id/bali/canggu",
            "id/bali/ubud",
            "id/bali",
            "th/bangkok/thonglor",
            "th/bangkok",
        }

    async def test_no_keys_means_no_query(self) -> None:
        repo = MagicMock(get_many=AsyncMock(return_value={}))

        assert await AreaHandleBuilder(area_repo=repo).for_keys([]) == {}
        repo.get_many.assert_not_awaited()
