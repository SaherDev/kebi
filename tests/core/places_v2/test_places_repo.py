"""Unit tests for PlacesRepo — SQL logic via mock AsyncSession."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect

from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceQuery,
)
from totoro_ai.core.places_v2.places_repo import PlacesRepo, _core_to_dict, _row_to_core
from totoro_ai.core.places_v2.tags import CuisineTag


def _make_repo(rows: list[dict] | None = None) -> tuple[PlacesRepo, MagicMock]:
    session = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(
        return_value=iter([MagicMock(_mapping=r) for r in (rows or [])])
    )
    mock_result.mappings = MagicMock(
        return_value=MagicMock(one=MagicMock(return_value={}))
    )
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()
    return PlacesRepo(session), session


def _minimal_row(
    pid: str = "p1",
    provider_id: str | None = "google:abc",
) -> dict:
    return {
        "id": pid,
        "provider_id": provider_id,
        "place_name": f"Place {pid}",
        "category": None,
        "tags": None,
        "location": None,
        "created_at": datetime.now(UTC),
        "refreshed_at": None,
    }


# ---------------------------------------------------------------------------
# _row_to_core
# ---------------------------------------------------------------------------

class TestRowToCore:
    def test_minimal_row(self) -> None:
        row = _minimal_row()
        core = _row_to_core(row)
        assert core.id == "p1"
        assert core.provider_id == "google:abc"
        assert core.place_name == "Place p1"
        assert core.tags == []
        assert core.location is None

    def test_with_tags(self) -> None:
        row = _minimal_row()
        row["tags"] = [{"type": "cuisine", "value": "Thai", "source": "google"}]
        core = _row_to_core(row)
        assert len(core.tags) == 1
        assert core.tags[0].value == "Thai"

    def test_with_location(self) -> None:
        row = _minimal_row()
        row["location"] = {"lat": 13.7, "lng": 100.5, "address": "Test St"}
        core = _row_to_core(row)
        assert core.location is not None
        assert core.location.lat == 13.7

    def test_with_category(self) -> None:
        row = _minimal_row()
        row["category"] = "restaurant"
        core = _row_to_core(row)
        assert core.category == PlaceCategory.restaurant

    def test_with_aliases(self) -> None:
        row = _minimal_row()
        row["place_name_aliases"] = [
            {"value": "Cafe Centro Mission", "source": "tiktok"},
            {"value": "el centro", "source": "user"},
        ]
        core = _row_to_core(row)
        assert len(core.place_name_aliases) == 2
        assert core.place_name_aliases[0].value == "Cafe Centro Mission"
        assert core.place_name_aliases[1].source == "user"

    def test_missing_aliases_key_yields_empty_list(self) -> None:
        # Old rows pre-column will be missing the key entirely.
        row = _minimal_row()
        row.pop("place_name_aliases", None)
        core = _row_to_core(row)
        assert core.place_name_aliases == []


# ---------------------------------------------------------------------------
# _core_to_dict
# ---------------------------------------------------------------------------

class TestCoreToDict:
    def test_generates_id_when_none(self) -> None:
        core = PlaceCore(place_name="Test", provider_id="google:x")
        now = datetime.now(UTC)
        d = _core_to_dict(core, now)
        assert d["id"] is not None
        assert isinstance(d["id"], str)

    def test_preserves_existing_id(self) -> None:
        core = PlaceCore(id="fixed-id", place_name="Test", provider_id="google:x")
        d = _core_to_dict(core, datetime.now(UTC))
        assert d["id"] == "fixed-id"

    def test_empty_tags_stored_as_none(self) -> None:
        core = PlaceCore(place_name="Test", provider_id="google:x")
        d = _core_to_dict(core, datetime.now(UTC))
        assert d["tags"] is None

    def test_empty_aliases_stored_as_none(self) -> None:
        core = PlaceCore(place_name="Test", provider_id="google:x")
        d = _core_to_dict(core, datetime.now(UTC))
        assert d["place_name_aliases"] is None

    def test_aliases_serialised(self) -> None:
        core = PlaceCore(
            place_name="Cafe Centro",
            provider_id="google:x",
            place_name_aliases=[
                PlaceNameAlias(value="el centro", source="user"),
                PlaceNameAlias(value="Cafe Centro Mission", source="tiktok"),
            ],
        )
        d = _core_to_dict(core, datetime.now(UTC))
        assert d["place_name_aliases"] == [
            {"value": "el centro", "source": "user"},
            {"value": "Cafe Centro Mission", "source": "tiktok"},
        ]

    def test_location_serialised(self) -> None:
        core = PlaceCore(
            place_name="Test",
            provider_id="google:x",
            location=LocationContext(lat=13.7, lng=100.5),
        )
        d = _core_to_dict(core, datetime.now(UTC))
        assert d["location"] == {"lat": 13.7, "lng": 100.5}


# ---------------------------------------------------------------------------
# get_by_ids
# ---------------------------------------------------------------------------

class TestGetByIds:
    async def test_empty_input_returns_empty(self) -> None:
        repo, session = _make_repo()
        result = await repo.get_by_ids([])
        assert result == []
        session.execute.assert_not_called()

    async def test_returns_parsed_cores(self) -> None:
        row = _minimal_row("a", "google:a")
        repo, _ = _make_repo([row])
        result = await repo.get_by_ids(["a"])
        assert len(result) == 1
        assert result[0].id == "a"


# ---------------------------------------------------------------------------
# get_by_provider_ids
# ---------------------------------------------------------------------------

class TestGetByProviderIds:
    async def test_empty_input_returns_empty(self) -> None:
        repo, session = _make_repo()
        result = await repo.get_by_provider_ids([])
        assert result == {}
        session.execute.assert_not_called()

    async def test_returns_dict_keyed_by_provider_id(self) -> None:
        row = _minimal_row("a", "google:a")
        repo, _ = _make_repo([row])
        result = await repo.get_by_provider_ids(["google:a"])
        assert "google:a" in result
        assert result["google:a"].provider_id == "google:a"


# ---------------------------------------------------------------------------
# find — filter conditions
# ---------------------------------------------------------------------------

class TestFind:
    async def test_empty_query_executes(self) -> None:
        repo, session = _make_repo([])
        result = await repo.find(PlaceQuery())
        assert result == []
        session.execute.assert_awaited_once()

    async def test_place_name_filter_applied(self) -> None:
        repo, session = _make_repo([])
        await repo.find(PlaceQuery(place_name="ramen"))
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "ramen" in compiled.lower()

    async def test_category_filter_applied(self) -> None:
        repo, session = _make_repo([])
        await repo.find(PlaceQuery(category=PlaceCategory.cafe))
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "cafe" in compiled.lower()

    async def test_tag_filter_applied(self) -> None:
        repo, session = _make_repo([])
        await repo.find(PlaceQuery(tags=[CuisineTag.thai]))
        stmt = session.execute.call_args.args[0]
        compiled = stmt.compile(dialect=pg_dialect.dialect())
        # JSONB containment operator present and "Thai" in bound parameter value
        assert "@>" in str(compiled)
        assert any("Thai" in str(v) for v in compiled.params.values())

    async def test_limit_passed(self) -> None:
        repo, session = _make_repo([])
        await repo.find(PlaceQuery(), limit=5)
        stmt = session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "5" in compiled


# ---------------------------------------------------------------------------
# upsert_places — provider_id enforcement + write semantics
# ---------------------------------------------------------------------------

class TestUpsertPlaces:
    async def test_empty_input_returns_empty(self) -> None:
        repo, session = _make_repo()
        result = await repo.upsert_places([])
        assert result == []
        session.execute.assert_not_called()

    async def test_raises_when_any_candidate_missing_provider_id(self) -> None:
        repo, session = _make_repo()
        cores = [
            PlaceCore(place_name="OK", provider_id="google:a"),
            PlaceCore(place_name="Orphan"),  # no provider_id
        ]
        with pytest.raises(ValueError, match="provider_id"):
            await repo.upsert_places(cores)
        session.execute.assert_not_called()
        session.commit.assert_not_called()

    async def test_set_clause_overwrites_mutable_columns(self) -> None:
        row = _minimal_row("x", "google:x")
        repo, session = _make_repo([row])

        await repo.upsert_places(
            [PlaceCore(place_name="Test", provider_id="google:x")]
        )

        stmt = session.execute.call_args.args[0]
        compiled = str(
            stmt.compile(
                dialect=pg_dialect.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        # Each mutable column appears in the DO UPDATE SET clause referencing
        # `excluded.<col>` (the candidate value), not the existing column.
        for col in (
            "place_name",
            "place_name_aliases",
            "category",
            "tags",
            "location",
            "refreshed_at",
        ):
            assert f"{col} = excluded.{col}" in compiled, (
                f"expected {col} = excluded.{col} in: {compiled}"
            )


class TestWipeStaleLocations:
    @staticmethod
    def _wipe_session(
        provider_ids: list[str | None],
    ) -> tuple[PlacesRepo, MagicMock]:
        rows = [
            _minimal_row(pid=f"row-{i}", provider_id=p)
            for i, p in enumerate(provider_ids)
        ]
        session = MagicMock()
        result = MagicMock()
        result.__iter__ = MagicMock(
            return_value=iter([MagicMock(_mapping=r) for r in rows])
        )
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        return PlacesRepo(session), session

    async def test_returns_wiped_cores(self) -> None:
        repo, session = self._wipe_session(["google:a", "google:b", "google:c"])
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)

        wiped = await repo.wipe_stale_locations(cutoff)

        assert [c.provider_id for c in wiped] == [
            "google:a",
            "google:b",
            "google:c",
        ]
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()

    async def test_returns_cores_with_null_provider_id_intact(self) -> None:
        """The repo doesn't filter NULL provider_ids — that's the service's call."""
        repo, _ = self._wipe_session(["google:a", None, "google:c"])
        wiped = await repo.wipe_stale_locations(datetime.now(UTC))
        assert [c.provider_id for c in wiped] == ["google:a", None, "google:c"]

    async def test_empty_result_returns_empty_list(self) -> None:
        repo, _ = self._wipe_session([])
        assert await repo.wipe_stale_locations(datetime.now(UTC)) == []

    async def test_compiled_sql_pins_set_where_and_returning(self) -> None:
        """Lock SET, WHERE, and RETURNING clauses against silent SQL drift."""
        repo, session = self._wipe_session([])
        cutoff = datetime(2026, 1, 1, tzinfo=UTC)

        await repo.wipe_stale_locations(cutoff)

        stmt = session.execute.call_args.args[0]
        compiled = str(
            stmt.compile(
                dialect=pg_dialect.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        flat = compiled.replace(" ", "")
        # SET clears both fields
        assert "location=NULL" in flat
        assert "refreshed_at=NULL" in flat
        # WHERE: idempotent guard + age cutoff
        assert "location IS NOT NULL" in compiled
        assert "refreshed_at <" in compiled
        # RETURNING all columns so the caller gets PlaceCore back
        assert "RETURNING" in compiled.upper()
        assert "provider_id" in compiled
        assert "place_name" in compiled

    async def test_commits_after_update(self) -> None:
        """A wipe must commit; otherwise the txn rolls back at session close."""
        repo, session = self._wipe_session(["google:a"])
        await repo.wipe_stale_locations(datetime.now(UTC))
        session.commit.assert_awaited_once()
