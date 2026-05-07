"""Tests for the recall repository (two-mode search, feature 019)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.recall.types import RecallFilters
from kebi.db.repositories.recall_repository import SQLAlchemyRecallRepository


def _row(
    place_id: str = "pid-1",
    place_name: str = "Cafe A",
    place_type: str = "food_and_drink",
    subcategory: str | None = "cafe",
    attributes: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    provider_id: str | None = "google:abc",
    matched_vector: bool = False,
    matched_text: bool = False,
    rrf_score: float | None = None,
) -> dict[str, Any]:
    return {
        "id": place_id,
        "place_name": place_name,
        "place_type": place_type,
        "subcategory": subcategory,
        "tags": tags or [],
        "attributes": attributes or {},
        "source_url": None,
        "source": source,
        "provider_id": provider_id,
        "created_at": datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
        "matched_vector": matched_vector,
        "matched_text": matched_text,
        "rrf_score": rrf_score,
    }


def _mock_session_with_rows(rows: list[dict[str, Any]], count: int = 0) -> AsyncMock:
    session = AsyncMock()

    result_mock = MagicMock()
    mappings_mock = MagicMock()
    mappings_mock.fetchall.return_value = rows
    result_mock.mappings.return_value = mappings_mock

    session.execute = AsyncMock(return_value=result_mock)
    session.scalar = AsyncMock(return_value=count)
    return session


# ---------------------------------------------------------------------------
# Filter mode (query is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_mode_runs_pure_select_and_returns_filter_match_reason() -> None:
    session = _mock_session_with_rows(
        [
            _row(place_id="p1", place_name="Coffee Lab"),
            _row(place_id="p2", place_name="Tiny Cafe"),
        ],
        count=2,
    )
    repo = SQLAlchemyRecallRepository(session)

    results, total_count = await repo.search(
        user_id="u1",
        query=None,
        filters=RecallFilters(place_type="food_and_drink"),
        sort_by="created_at",
        limit=10,
    )

    assert total_count == 2
    assert [r.place.place_id for r in results] == ["p1", "p2"]
    assert all(r.match_reason == "filter" for r in results)
    assert all(r.relevance_score is None for r in results)


@pytest.mark.asyncio
async def test_filter_mode_empty_filters_only_has_user_id_clause() -> None:
    session = _mock_session_with_rows([], count=0)
    repo = SQLAlchemyRecallRepository(session)

    await repo.search(user_id="u1", query=None, filters=RecallFilters(), limit=10)

    # The SELECT call is the first execute call; the COUNT is a scalar call.
    select_call = session.execute.call_args_list[0]
    params = select_call.args[1]
    assert params["user_id"] == "u1"
    assert "place_type" not in params
    assert "cuisine" not in params
    # Scalar count was called once with the same user_id.
    scalar_call = session.scalar.call_args_list[0]
    assert scalar_call.args[1]["user_id"] == "u1"


@pytest.mark.asyncio
async def test_filter_mode_builds_where_clauses_for_every_field() -> None:
    session = _mock_session_with_rows([], count=0)
    repo = SQLAlchemyRecallRepository(session)

    from kebi.core.places.models import LocationContext, PlaceAttributes

    filters = RecallFilters(
        place_type="food_and_drink",
        subcategory="cafe",
        source="tiktok",
        tags_include=["date-night"],
        attributes=PlaceAttributes(
            cuisine="japanese",
            price_hint="moderate",
            ambiance="cozy",
            location_context=LocationContext(
                neighborhood="Siam", city="Bangkok", country="Thailand"
            ),
        ),
    )
    await repo.search(user_id="u1", query=None, filters=filters, limit=10)

    params = session.execute.call_args_list[0].args[1]
    assert params["place_type"] == "food_and_drink"
    assert params["subcategory"] == "cafe"
    assert params["source"] == "tiktok"
    assert params["cuisine"] == "japanese"
    assert params["price_hint"] == "moderate"
    assert params["ambiance"] == "cozy"
    assert params["neighborhood"] == "Siam"
    assert params["city"] == "Bangkok"
    assert params["country"] == "Thailand"
    # tags_include is JSON-serialized for @> jsonb containment.
    assert params["tags_include"] == '["date-night"]'

    sql_text = str(session.execute.call_args_list[0].args[0])
    assert "p.place_type = :place_type" in sql_text
    assert "p.attributes->>'cuisine' = :cuisine" in sql_text
    assert "p.attributes->'location_context'->>'city' = :city" in sql_text
    assert "p.tags @> :tags_include::jsonb" in sql_text


@pytest.mark.asyncio
async def test_total_count_round_trip() -> None:
    session = _mock_session_with_rows(
        [_row(place_id="p1")],
        count=147,
    )
    repo = SQLAlchemyRecallRepository(session)

    results, total_count = await repo.search(
        user_id="u1", query=None, filters=RecallFilters(), limit=10
    )

    assert len(results) == 1
    assert total_count == 147


# ---------------------------------------------------------------------------
# Hybrid mode (query is not None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_mode_with_vector_uses_search_vector_column() -> None:
    session = _mock_session_with_rows(
        [_row(matched_vector=True, matched_text=True, rrf_score=0.8)],
        count=1,
    )
    repo = SQLAlchemyRecallRepository(session)

    results, _total = await repo.search(
        user_id="u1",
        query="ramen spot",
        query_vector=[0.1] * 1024,
        filters=RecallFilters(),
        sort_by="relevance",
        limit=10,
    )

    assert results[0].match_reason == "semantic + keyword"
    assert results[0].relevance_score == 0.8
    # The generated `search_vector` column is referenced directly — never
    # as inline `to_tsvector(...)`.
    sql_text = str(session.execute.call_args_list[0].args[0])
    assert "p.search_vector" in sql_text
    assert "to_tsvector" not in sql_text


@pytest.mark.asyncio
async def test_hybrid_mode_vector_only_maps_match_reason() -> None:
    session = _mock_session_with_rows(
        [_row(matched_vector=True, matched_text=False, rrf_score=0.4)],
        count=1,
    )
    repo = SQLAlchemyRecallRepository(session)

    results, _ = await repo.search(
        user_id="u1",
        query="q",
        query_vector=[0.1],
        filters=RecallFilters(),
        limit=10,
    )

    assert results[0].match_reason == "semantic"


@pytest.mark.asyncio
async def test_hybrid_mode_keyword_only_maps_match_reason() -> None:
    session = _mock_session_with_rows(
        [_row(matched_vector=False, matched_text=True, rrf_score=0.2)],
        count=1,
    )
    repo = SQLAlchemyRecallRepository(session)

    results, _ = await repo.search(
        user_id="u1",
        query="q",
        query_vector=[0.1],
        filters=RecallFilters(),
        limit=10,
    )

    assert results[0].match_reason == "keyword"


@pytest.mark.asyncio
async def test_hybrid_mode_falls_back_to_fts_only_without_vector() -> None:
    session = _mock_session_with_rows(
        [_row(matched_text=True, rrf_score=0.1)],
        count=1,
    )
    repo = SQLAlchemyRecallRepository(session)

    await repo.search(
        user_id="u1",
        query="ramen",
        query_vector=None,
        filters=RecallFilters(),
        limit=10,
    )

    sql_text = str(session.execute.call_args_list[0].args[0])
    # FTS-only path uses ts_rank against the generated column.
    assert "p.search_vector" in sql_text
    assert "ts_rank" in sql_text
    # The hybrid CTE is not assembled.
    assert "vector_results" not in sql_text


@pytest.mark.asyncio
async def test_hybrid_mode_applies_filter_where_clauses() -> None:
    session = _mock_session_with_rows([], count=0)
    repo = SQLAlchemyRecallRepository(session)

    from kebi.core.places.models import PlaceAttributes

    filters = RecallFilters(
        place_type="food_and_drink",
        attributes=PlaceAttributes(cuisine="japanese"),
    )
    await repo.search(
        user_id="u1",
        query="ramen",
        query_vector=[0.1],
        filters=filters,
        limit=10,
    )

    params = session.execute.call_args_list[0].args[1]
    assert params["place_type"] == "food_and_drink"
    assert params["cuisine"] == "japanese"
    assert params["query_text"] == "ramen"
    assert "query_vector" in params


# ---------------------------------------------------------------------------
# count_saved_places
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_saved_places_returns_scalar_value() -> None:
    session = _mock_session_with_rows([], count=42)
    repo = SQLAlchemyRecallRepository(session)

    count = await repo.count_saved_places("u1")

    assert count == 42


@pytest.mark.asyncio
async def test_count_saved_places_returns_zero_for_new_user() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    repo = SQLAlchemyRecallRepository(session)

    count = await repo.count_saved_places("new-user")

    assert count == 0
