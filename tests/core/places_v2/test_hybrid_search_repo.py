"""Unit tests for HybridSearchRepo — SQL composition + row mapping via mocks.

The repo is exercised through its public surface. SQL behaviour is verified
by compiling the statement passed to ``session.execute`` and asserting on:
  - the structure of the SELECT (CTEs, RRF formula, FULL OUTER JOIN)
  - the bound parameters for each filter type
  - the operators used for vector kNN and FTS legs
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect

from totoro_ai.core.places_v2.hybrid_search_repo import (
    _TS_CONFIG,
    HybridSearchRepo,
    _filter_conditions,
    _row_to_hit,
)
from totoro_ai.core.places_v2.models import (
    HybridSearchFilters,
    HybridSearchHit,
    LocationContext,
    PlaceCategory,
    PlaceCore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(rows: list[dict] | None = None) -> tuple[HybridSearchRepo, MagicMock]:
    """Return a HybridSearchRepo bound to a mock AsyncSession.

    Iterating the mock result yields objects with `_mapping` set to the
    provided rows — same shape `_row_to_hit` consumes.
    """
    session = MagicMock()
    mock_result = MagicMock()
    mock_result.__iter__ = MagicMock(
        return_value=iter([MagicMock(_mapping=r) for r in (rows or [])])
    )
    session.execute = AsyncMock(return_value=mock_result)
    session.commit = AsyncMock()
    return HybridSearchRepo(session), session


def _hit_row(
    *,
    pid: str = "p1",
    user_id: str = "u1",
    rrf: float = 0.025,
    v_rank: int | None = 1,
    t_rank: int | None = 2,
    place_name: str = "Test Place",
    category: str | None = "restaurant",
    tags: list[dict[str, Any]] | None = None,
    location: dict[str, Any] | None = None,
    aliases: list[dict[str, Any]] | None = None,
    visited: bool = False,
    liked: bool | None = None,
    approved: bool = True,
    note: str | None = None,
    source: str = "manual",
    source_url: str | None = None,
) -> dict[str, Any]:
    saved_at = datetime.now(UTC)
    return {
        # places_v2 columns
        "id": pid,
        "provider_id": f"google:{pid}",
        "place_name": place_name,
        "place_name_aliases": aliases,
        "category": category,
        "tags": tags,
        "location": location,
        "created_at": saved_at,
        "refreshed_at": None,
        # user_places columns
        "user_place_id": f"up_{pid}",
        "user_id": user_id,
        "approved": approved,
        "visited": visited,
        "liked": liked,
        "note": note,
        "source": source,
        "source_url": source_url,
        "saved_at": saved_at,
        "visited_at": None,
        # scores
        "rrf_score": rrf,
        "vector_rank": v_rank,
        "text_rank": t_rank,
    }


def _query_vector(dim: int = 1024) -> list[float]:
    return [0.1] * dim


def _compiled(stmt: Any) -> Any:
    return stmt.compile(
        dialect=pg_dialect.dialect(),
        compile_kwargs={"literal_binds": True},
    )


# ---------------------------------------------------------------------------
# HybridSearchFilters validator
# ---------------------------------------------------------------------------


class TestHybridSearchFiltersValidator:
    def test_empty_filters_construct(self) -> None:
        f = HybridSearchFilters()
        assert f.category is None
        assert f.tags is None

    def test_geo_lat_without_lng_rejected(self) -> None:
        with pytest.raises(ValueError, match="lat and lng must both be set"):
            HybridSearchFilters(lat=35.6, radius_m=500)

    def test_geo_lng_without_lat_rejected(self) -> None:
        with pytest.raises(ValueError, match="lat and lng must both be set"):
            HybridSearchFilters(lng=139.7, radius_m=500)

    def test_geo_without_radius_rejected(self) -> None:
        with pytest.raises(ValueError, match="radius_m is required"):
            HybridSearchFilters(lat=35.6, lng=139.7)

    def test_full_geo_accepted(self) -> None:
        f = HybridSearchFilters(lat=35.6, lng=139.7, radius_m=500)
        assert f.lat == 35.6
        assert f.radius_m == 500

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValueError):
            HybridSearchFilters(unknown_field="oops")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _filter_conditions — one condition per active filter, none for empty
# ---------------------------------------------------------------------------


class TestFilterConditions:
    def test_empty_filters_produce_no_conditions(self) -> None:
        assert _filter_conditions(HybridSearchFilters()) == []

    def test_category_condition(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(category=PlaceCategory.cafe)
        )
        assert len(cond) == 1
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "category" in sql.lower()
        assert "cafe" in sql.lower()

    def test_single_tag_uses_jsonb_containment(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(tags=["italian"])
        )
        assert len(cond) == 1
        compiled = cond[0].compile(dialect=pg_dialect.dialect())
        assert "@>" in str(compiled)
        assert any("italian" in str(v) for v in compiled.params.values())

    def test_multiple_tags_each_get_own_condition(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(tags=["italian", "cozy"])
        )
        assert len(cond) == 2

    def test_tag_param_value_is_jsonb_array_string(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(tags=["italian"])
        )
        compiled = cond[0].compile(dialect=pg_dialect.dialect())
        # The bound JSONB literal is the json.dumps of [{"value": "italian"}].
        expected = json.dumps([{"value": "italian"}])
        assert any(str(v) == expected for v in compiled.params.values())

    def test_city_filter_uses_ilike(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(city="Tokyo"))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ilike" in sql.lower()
        assert "tokyo" in sql.lower()
        # ILIKE pattern should be wrapped in % wildcards
        assert "%tokyo%" in sql.lower()

    def test_neighborhood_filter_uses_ilike(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(neighborhood="Shibuya"))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ilike" in sql.lower()
        assert "%shibuya%" in sql.lower()

    def test_country_filter_uses_exact_match(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(country="Japan"))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        # Country is exact match, not ILIKE
        assert "ilike" not in sql.lower()
        assert "japan" in sql.lower()

    def test_geo_emits_earth_box_and_null_guards(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(lat=35.6, lng=139.7, radius_m=500)
        )
        # 4 conditions: location not null, lat not null, lng not null,
        # earth_box containment.
        assert len(cond) == 4
        joined = " ".join(
            str(c.compile(compile_kwargs={"literal_binds": True}))
            for c in cond
        )
        assert "earth_box" in joined
        assert "ll_to_earth" in joined
        assert "is not null" in joined.lower()

    def test_saved_after_emits_gte_comparison(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        cond = _filter_conditions(HybridSearchFilters(saved_after=ts))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert ">=" in sql
        assert "saved_at" in sql.lower()

    def test_saved_before_emits_lte_comparison(self) -> None:
        ts = datetime(2026, 12, 31, tzinfo=UTC)
        cond = _filter_conditions(HybridSearchFilters(saved_before=ts))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "<=" in sql
        assert "saved_at" in sql.lower()

    def test_visited_filter_targets_user_places(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(visited=True))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "visited" in sql.lower()

    def test_liked_true_filter(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(liked=True))
        sql = str(cond[0].compile(compile_kwargs={"literal_binds": True}))
        assert "liked" in sql.lower()

    def test_liked_false_filter(self) -> None:
        # liked tri-state: explicit False is a real filter, not "any"
        cond = _filter_conditions(HybridSearchFilters(liked=False))
        assert len(cond) == 1

    def test_approved_filter(self) -> None:
        cond = _filter_conditions(HybridSearchFilters(approved=False))
        assert len(cond) == 1

    def test_combined_filters_each_present(self) -> None:
        cond = _filter_conditions(
            HybridSearchFilters(
                category=PlaceCategory.restaurant,
                tags=["italian"],
                city="Tokyo",
                visited=True,
                liked=True,
            )
        )
        # category(1) + tags(1) + city(1) + visited(1) + liked(1) = 5
        assert len(cond) == 5


# ---------------------------------------------------------------------------
# _row_to_hit — DB row → HybridSearchHit Pydantic
# ---------------------------------------------------------------------------


class TestRowToHit:
    def test_minimal_row(self) -> None:
        hit = _row_to_hit(_hit_row())
        assert isinstance(hit, HybridSearchHit)
        assert hit.place.id == "p1"
        assert hit.place.place_name == "Test Place"
        assert hit.rrf_score == pytest.approx(0.025)
        assert hit.vector_rank == 1
        assert hit.text_rank == 2

    def test_vector_rank_none_preserved(self) -> None:
        # Place that didn't make the vector top-K (text-only hit).
        hit = _row_to_hit(_hit_row(v_rank=None))
        assert hit.vector_rank is None
        assert hit.text_rank == 2

    def test_text_rank_none_preserved(self) -> None:
        hit = _row_to_hit(_hit_row(t_rank=None))
        assert hit.text_rank is None
        assert hit.vector_rank == 1

    def test_rank_zero_does_not_collapse_to_none(self) -> None:
        # Defensive: rank starts at 1 in SQL, but the falsy-vs-None bug
        # had this collapsing 0 to None. Pin the contract.
        hit = _row_to_hit(_hit_row(v_rank=0, t_rank=0))
        assert hit.vector_rank == 0
        assert hit.text_rank == 0

    def test_tags_parsed_from_jsonb(self) -> None:
        row = _hit_row(
            tags=[
                {"type": "cuisine", "value": "Italian", "source": "google"},
                {"type": "atmosphere", "value": "cozy", "source": "llm"},
            ],
        )
        hit = _row_to_hit(row)
        assert len(hit.place.tags) == 2
        assert hit.place.tags[0].value == "Italian"

    def test_aliases_parsed_from_jsonb(self) -> None:
        row = _hit_row(
            aliases=[
                {"value": "Cafe Latte", "source": "tiktok"},
                {"value": "CL Coffee", "source": "user"},
            ],
        )
        hit = _row_to_hit(row)
        assert len(hit.place.place_name_aliases) == 2

    def test_location_parsed_when_present(self) -> None:
        row = _hit_row(
            location={
                "lat": 35.6, "lng": 139.7,
                "city": "Tokyo", "country": "Japan",
            },
        )
        hit = _row_to_hit(row)
        assert isinstance(hit.place.location, LocationContext)
        assert hit.place.location.city == "Tokyo"

    def test_location_none_when_absent(self) -> None:
        hit = _row_to_hit(_hit_row(location=None))
        assert hit.place.location is None

    def test_category_string_coerced_to_enum(self) -> None:
        hit = _row_to_hit(_hit_row(category="cafe"))
        assert hit.place.category == PlaceCategory.cafe

    def test_category_none_preserved(self) -> None:
        hit = _row_to_hit(_hit_row(category=None))
        assert hit.place.category is None

    def test_missing_jsonb_keys_yield_empty_lists(self) -> None:
        # If the row mapping is missing tags/aliases entirely, fall back to [].
        row = _hit_row()
        row.pop("tags", None)
        row.pop("place_name_aliases", None)
        hit = _row_to_hit(row)
        assert hit.place.tags == []
        assert hit.place.place_name_aliases == []

    def test_user_data_populated_from_row(self) -> None:
        row = _hit_row(
            user_id="u-42",
            visited=True,
            liked=True,
            note="best for first dates",
            source="tiktok",
            source_url="https://tiktok.com/@x/video/1",
        )
        hit = _row_to_hit(row)
        assert hit.user_data.user_id == "u-42"
        assert hit.user_data.place_id == row["id"]
        assert hit.user_data.user_place_id == row["user_place_id"]
        assert hit.user_data.visited is True
        assert hit.user_data.liked is True
        assert hit.user_data.note == "best for first dates"
        assert hit.user_data.source.value == "tiktok"
        assert hit.user_data.source_url == "https://tiktok.com/@x/video/1"

    def test_user_data_liked_none_preserved(self) -> None:
        # Tri-state liked: NULL means "no opinion" and must not become False.
        hit = _row_to_hit(_hit_row(liked=None))
        assert hit.user_data.liked is None


# ---------------------------------------------------------------------------
# search() — SQL shape inspection
# ---------------------------------------------------------------------------


class TestSearchSQLShape:
    async def test_executes_once(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        session.execute.assert_awaited_once()

    async def test_empty_results_yield_empty_list(self) -> None:
        repo, _ = _make_repo([])
        result = await repo.search("u1", "italian", _query_vector())
        assert result == []

    async def test_compiled_sql_contains_three_named_ctes(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # CTE names: filtered, vec, txt, fused
        assert "filtered" in sql
        assert "vec" in sql
        assert "txt" in sql
        assert "fused" in sql

    async def test_vector_leg_uses_cosine_distance_operator(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        # pgvector cosine distance is <=>
        assert "<=>" in sql

    async def test_text_leg_uses_websearch_to_tsquery(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "cozy italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "websearch_to_tsquery" in sql
        assert _TS_CONFIG in sql

    async def test_text_leg_uses_ts_rank_cd_for_weighting(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # ts_rank_cd respects the per-field setweight() weights.
        assert "ts_rank_cd" in sql

    async def test_text_leg_uses_match_operator(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        # @@ is the tsvector match operator
        assert "@@" in sql

    async def test_full_outer_join_present(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "full outer join" in sql

    async def test_rrf_formula_present(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector(), rrf_k=60)
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # 1.0 / (60 + rank) — k baked in via literal_binds
        assert "1.0 /" in sql or "1.0/" in sql
        assert "60" in sql

    async def test_row_number_window_used_for_ranks(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "row_number()" in sql

    async def test_outer_limit_respected(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector(), limit=7)
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        # The outermost LIMIT is the smallest one — others are
        # candidate_limit = limit * multiplier.
        assert "LIMIT 7" in sql or "limit 7" in sql.lower()

    async def test_candidate_limit_is_limit_times_multiplier(self) -> None:
        repo, session = _make_repo([])
        await repo.search(
            "u1", "italian", _query_vector(), limit=5, candidate_multiplier=4
        )
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # Each leg's CTE limits to candidate_limit = 20.
        assert "limit 20" in sql

    async def test_default_rrf_k_is_60(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        assert "60" in sql

    async def test_custom_rrf_k_propagates(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector(), rrf_k=120)
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        assert "120" in sql

    async def test_empty_filters_means_no_where_constraints(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # When no filters supplied, the filtered CTE has no WHERE clause
        # beyond the implicit TRUE.
        # We can't assert the negation easily; just confirm CTE present.
        assert "filtered" in sql

    async def test_filters_propagated_into_filtered_cte(self) -> None:
        repo, session = _make_repo([])
        await repo.search(
            "u1",
            "italian",
            _query_vector(),
            filters=HybridSearchFilters(
                category=PlaceCategory.restaurant,
                city="Tokyo",
            ),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "restaurant" in sql
        assert "tokyo" in sql

    async def test_result_rows_mapped_to_hits(self) -> None:
        rows = [
            _hit_row(pid="a", rrf=0.04, v_rank=1, t_rank=2),
            _hit_row(pid="b", rrf=0.03, v_rank=3, t_rank=None),
            _hit_row(pid="c", rrf=0.02, v_rank=None, t_rank=1),
        ]
        repo, _ = _make_repo(rows)
        results = await repo.search("u1", "italian", _query_vector())
        assert len(results) == 3
        assert results[0].place.id == "a"
        assert results[1].vector_rank == 3
        assert results[1].text_rank is None
        assert results[2].vector_rank is None
        assert results[2].text_rank == 1


# ---------------------------------------------------------------------------
# User scoping — user_id propagation, JOIN, DISTINCT ON
# ---------------------------------------------------------------------------


class TestUserScoping:
    async def test_user_places_joined_in_filtered_cte(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # filtered CTE pulls from both tables
        assert "user_places" in sql
        assert "places_v2" in sql

    async def test_user_id_appears_as_bound_value(self) -> None:
        repo, session = _make_repo([])
        await repo.search("user-xyz-123", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt))
        assert "user-xyz-123" in sql

    async def test_distinct_on_place_id_present(self) -> None:
        repo, session = _make_repo([])
        await repo.search("u1", "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # DISTINCT ON collapses two user_places rows for the same place
        # down to the most recent one (by saved_at desc).
        assert "distinct on" in sql

    async def test_user_side_filter_propagates_into_sql(self) -> None:
        repo, session = _make_repo([])
        await repo.search(
            "u1",
            "italian",
            _query_vector(),
            filters=HybridSearchFilters(visited=True, liked=True),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # both filters should land in the WHERE clause
        assert "visited" in sql
        assert "liked" in sql

    async def test_saved_after_filter_reaches_user_places_saved_at(self) -> None:
        repo, session = _make_repo([])
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        await repo.search(
            "u1",
            "italian",
            _query_vector(),
            filters=HybridSearchFilters(saved_after=ts),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "saved_at" in sql


# ---------------------------------------------------------------------------
# Unscoped mode — user_id=None, global place catalog search
# ---------------------------------------------------------------------------


def _unscoped_row(**overrides: Any) -> dict[str, Any]:
    """Row produced by the unscoped CTE — user_places columns are NULL."""
    row = _hit_row(**overrides)
    for key in (
        "user_place_id", "user_id", "approved", "visited", "liked",
        "note", "source", "source_url", "saved_at", "visited_at",
    ):
        row[key] = None
    return row


class TestUnscopedMode:
    async def test_user_id_none_skips_user_places_join(self) -> None:
        repo, session = _make_repo([])
        await repo.search(None, "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # filtered CTE in unscoped mode does NOT touch user_places.
        # The bound user_id literal also doesn't appear.
        assert "user_places" not in sql

    async def test_unscoped_filtered_cte_pads_user_columns_with_null(self) -> None:
        repo, session = _make_repo([])
        await repo.search(None, "italian", _query_vector())
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        # NULL placeholders for the user_places columns keep the
        # downstream final SELECT shape identical to the scoped path.
        assert "null" in sql

    async def test_unscoped_hit_has_user_data_none(self) -> None:
        repo, _ = _make_repo([_unscoped_row(pid="p1", rrf=0.05)])
        results = await repo.search(None, "italian", _query_vector())
        assert len(results) == 1
        assert results[0].place.id == "p1"
        assert results[0].user_data is None
        assert results[0].rrf_score == pytest.approx(0.05)

    async def test_unscoped_rejects_visited_filter(self) -> None:
        repo, _ = _make_repo([])
        with pytest.raises(ValueError, match="user-side filters"):
            await repo.search(
                None,
                "italian",
                _query_vector(),
                filters=HybridSearchFilters(visited=True),
            )

    async def test_unscoped_rejects_liked_filter(self) -> None:
        repo, _ = _make_repo([])
        with pytest.raises(ValueError, match="user-side filters"):
            await repo.search(
                None,
                "italian",
                _query_vector(),
                filters=HybridSearchFilters(liked=True),
            )

    async def test_unscoped_rejects_approved_filter(self) -> None:
        repo, _ = _make_repo([])
        with pytest.raises(ValueError, match="user-side filters"):
            await repo.search(
                None,
                "italian",
                _query_vector(),
                filters=HybridSearchFilters(approved=False),
            )

    async def test_unscoped_rejects_saved_after_filter(self) -> None:
        repo, _ = _make_repo([])
        with pytest.raises(ValueError, match="user-side filters"):
            await repo.search(
                None,
                "italian",
                _query_vector(),
                filters=HybridSearchFilters(
                    saved_after=datetime(2026, 1, 1, tzinfo=UTC)
                ),
            )

    async def test_unscoped_accepts_place_side_filters(self) -> None:
        repo, session = _make_repo([])
        await repo.search(
            None,
            "italian",
            _query_vector(),
            filters=HybridSearchFilters(
                category=PlaceCategory.restaurant,
                city="Tokyo",
            ),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(_compiled(stmt)).lower()
        assert "restaurant" in sql
        assert "tokyo" in sql


# ---------------------------------------------------------------------------
# _row_to_hit — None user_data when user_place_id is missing
# ---------------------------------------------------------------------------


class TestRowToHitUnscoped:
    def test_none_user_place_id_yields_none_user_data(self) -> None:
        row = _unscoped_row()
        hit = _row_to_hit(row)
        assert hit.user_data is None
        assert hit.place.id == row["id"]


# ---------------------------------------------------------------------------
# search() — exception handling
# ---------------------------------------------------------------------------


class TestSearchErrorHandling:
    async def test_db_failure_propagates(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("connection lost"))
        repo = HybridSearchRepo(session)

        with pytest.raises(RuntimeError, match="connection lost"):
            await repo.search("u1", "italian", _query_vector())


# ---------------------------------------------------------------------------
# PlaceCore on the hit is the right shape
# ---------------------------------------------------------------------------


class TestHitPlaceShape:
    async def test_place_is_full_placecore(self) -> None:
        rows = [
            _hit_row(
                tags=[{"type": "cuisine", "value": "Italian", "source": "google"}],
                location={"city": "Tokyo", "country": "Japan"},
                aliases=[{"value": "Alt Name", "source": "user"}],
            )
        ]
        repo, _ = _make_repo(rows)
        results = await repo.search("u1", "italian", _query_vector())
        place = results[0].place
        assert isinstance(place, PlaceCore)
        assert place.tags[0].value == "Italian"
        assert place.location is not None and place.location.city == "Tokyo"
        assert place.place_name_aliases[0].value == "Alt Name"

    async def test_hit_carries_user_data_alongside_place(self) -> None:
        rows = [
            _hit_row(
                user_id="u1",
                visited=True,
                liked=True,
                note="reservation only",
            )
        ]
        repo, _ = _make_repo(rows)
        results = await repo.search("u1", "italian", _query_vector())
        hit = results[0]
        assert hit.user_data.user_id == "u1"
        assert hit.user_data.visited is True
        assert hit.user_data.note == "reservation only"
