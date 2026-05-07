"""Recall repository — two-mode search over the reshaped `places` table.

ADR-054 / feature 019: the repository now speaks `PlaceObject` and takes a
`RecallFilters` set. One entry point `search()` handles two modes:

- **filter mode** (`query is None`): pure `SELECT ... WHERE ... ORDER BY
  created_at DESC LIMIT`, no embedding, no FTS, no RRF.
- **hybrid mode** (`query is not None`): vector similarity over `embeddings`
  + FTS on the generated `places.search_vector` column + RRF merge, with
  the same `WHERE` clauses applied to both CTEs.

Distance filtering is **not** a SQL concern — the `location` parameter is
accepted (so the interface matches the recall service's call shape) but
ignored in the repository body. The recall service applies the haversine
filter in Python after calling `PlacesService.enrich_batch(geo_only=True)`.

Every `WHERE` clause is bound by name; no string interpolation of user
data. The `place_name`/`attributes` shape is Tier 1 only — the repository
never touches `lat`, `lng`, `address`, or any cache tier.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.places.models import (
    PlaceAttributes,
    PlaceObject,
    PlaceSource,
    PlaceType,
)
from kebi.core.recall.types import RecallFilters, RecallResult

logger = logging.getLogger(__name__)


class RecallRepository(Protocol):
    """Protocol for recall search operations."""

    async def search(
        self,
        user_id: str,
        query: str | None,
        query_vector: list[float] | None,
        filters: RecallFilters,
        sort_by: Literal["relevance", "created_at"],
        limit: int,
        rrf_k: int = 60,
        candidate_multiplier: int = 2,
        min_rrf_score: float = 0.01,
        max_cosine_distance: float = 0.65,
        location: tuple[float, float] | None = None,
    ) -> tuple[list[RecallResult], int]:
        """Return (results, total_count). `total_count` is the WHERE-clause
        match count without the LIMIT. `location` is accepted but ignored;
        the recall service handles distance filtering after enrich_batch.
        """
        ...

    async def count_saved_places(self, user_id: str) -> int: ...


class SQLAlchemyRecallRepository:
    """Concrete recall repository over the Tier-1 `places` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    async def count_saved_places(self, user_id: str) -> int:
        sql = text("SELECT COUNT(*) FROM places WHERE user_id = :user_id")
        result = await self._session.scalar(sql, {"user_id": user_id})
        return int(result or 0)

    async def search(
        self,
        user_id: str,
        query: str | None,
        query_vector: list[float] | None = None,
        filters: RecallFilters | None = None,
        sort_by: Literal["relevance", "created_at"] = "relevance",
        limit: int = 10,
        rrf_k: int = 60,
        candidate_multiplier: int = 2,
        min_rrf_score: float = 0.01,
        max_cosine_distance: float = 0.65,
        location: tuple[float, float] | None = None,
    ) -> tuple[list[RecallResult], int]:
        del location  # handled post-enrichment in the recall service
        filters = filters or RecallFilters()
        where_sql, params = self._build_where_clause(user_id, filters)

        try:
            if query is None:
                results = await self._filter_mode(where_sql, params, sort_by, limit)
            else:
                results = await self._hybrid_mode(
                    query=query,
                    query_vector=query_vector,
                    where_sql=where_sql,
                    params=params,
                    sort_by=sort_by,
                    limit=limit,
                    rrf_k=rrf_k,
                    candidate_multiplier=candidate_multiplier,
                    min_rrf_score=min_rrf_score,
                    max_cosine_distance=max_cosine_distance,
                )
            total_count = await self._total_count(where_sql, params)
        except Exception as exc:
            logger.error(
                "recall.search.failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            raise RuntimeError(f"Failed to perform recall search: {exc}") from exc

        return results, total_count

    # ------------------------------------------------------------------
    # Filter mode
    # ------------------------------------------------------------------
    async def _filter_mode(
        self,
        where_sql: str,
        params: dict[str, Any],
        sort_by: Literal["relevance", "created_at"],
        limit: int,
    ) -> list[RecallResult]:
        # In filter mode, "relevance" has no signal — fall back to created_at.
        del sort_by
        sql = text(
            f"""
            SELECT
                p.id,
                p.place_name,
                p.place_type,
                p.subcategory,
                p.tags,
                p.attributes,
                p.source_url,
                p.source,
                p.provider_id,
                p.created_at
            FROM places p
            WHERE {where_sql}
            ORDER BY p.created_at DESC
            LIMIT :limit
            """
        )
        exec_params = {**params, "limit": limit}
        result = await self._session.execute(sql, exec_params)
        rows = result.mappings().fetchall()
        return [
            RecallResult(
                place=self._row_to_place_object(row),
                match_reason="filter",
                relevance_score=None,
                score_type=None,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Hybrid mode
    # ------------------------------------------------------------------
    async def _hybrid_mode(
        self,
        query: str,
        query_vector: list[float] | None,
        where_sql: str,
        params: dict[str, Any],
        sort_by: Literal["relevance", "created_at"],
        limit: int,
        rrf_k: int,
        candidate_multiplier: int,
        min_rrf_score: float,
        max_cosine_distance: float,
    ) -> list[RecallResult]:
        candidate_limit = limit * candidate_multiplier
        exec_params: dict[str, Any] = {
            **params,
            "query_text": query,
            "limit": limit,
            "rrf_k": rrf_k,
            "candidate_limit": candidate_limit,
            "min_rrf_score": min_rrf_score,
            "max_cosine_distance": max_cosine_distance,
        }

        score_type: Literal["rrf", "ts_rank"]
        if query_vector is not None:
            exec_params["query_vector"] = (
                "[" + ",".join(str(v) for v in query_vector) + "]"
            )
            sql = self._build_hybrid_sql(where_sql, sort_by=sort_by)
            score_type = "rrf"
        else:
            sql = self._build_fts_only_sql(where_sql, sort_by=sort_by)
            score_type = "ts_rank"

        result = await self._session.execute(sql, exec_params)
        rows = result.mappings().fetchall()
        return [
            RecallResult(
                place=self._row_to_place_object(row),
                match_reason=self._match_reason_from_row(row),
                relevance_score=(
                    float(row["rrf_score"])
                    if row.get("rrf_score") is not None
                    else None
                ),
                score_type=score_type,
            )
            for row in rows
        ]

    def _build_hybrid_sql(
        self, where_sql: str, sort_by: Literal["relevance", "created_at"]
    ) -> Any:
        order = (
            "ORDER BY c.rrf_score DESC"
            if sort_by == "relevance"
            else "ORDER BY p.created_at DESC"
        )
        return text(
            f"""
            WITH vector_results AS (
                SELECT
                    p.id,
                    ROW_NUMBER() OVER (ORDER BY e.vector <=> :query_vector) AS rank
                FROM places p
                JOIN embeddings e ON e.place_id = p.id
                WHERE {where_sql}
                  AND e.vector <=> :query_vector < :max_cosine_distance
                ORDER BY e.vector <=> :query_vector
                LIMIT :candidate_limit
            ),
            text_results AS (
                SELECT
                    p.id,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank(p.search_vector, plainto_tsquery('english', :query_text)) DESC
                    ) AS rank
                FROM places p
                WHERE {where_sql}
                  AND p.search_vector @@ plainto_tsquery('english', :query_text)
                LIMIT :candidate_limit
            ),
            combined AS (
                SELECT
                    COALESCE(vr.id, tr.id) AS id,
                    COALESCE(1.0 / (:rrf_k + vr.rank), 0) +
                        COALESCE(1.0 / (:rrf_k + tr.rank), 0) AS rrf_score,
                    (vr.id IS NOT NULL) AS matched_vector,
                    (tr.id IS NOT NULL) AS matched_text
                FROM vector_results vr
                FULL OUTER JOIN text_results tr ON vr.id = tr.id
            )
            SELECT
                p.id,
                p.place_name,
                p.place_type,
                p.subcategory,
                p.tags,
                p.attributes,
                p.source_url,
                p.source,
                p.provider_id,
                p.created_at,
                c.rrf_score,
                c.matched_vector,
                c.matched_text
            FROM combined c
            JOIN places p ON p.id = c.id
            WHERE c.rrf_score >= :min_rrf_score
            {order}
            LIMIT :limit
            """
        )

    def _build_fts_only_sql(
        self, where_sql: str, sort_by: Literal["relevance", "created_at"]
    ) -> Any:
        order = (
            "ORDER BY ts_rank(p.search_vector, plainto_tsquery('english', :query_text)) DESC"
            if sort_by == "relevance"
            else "ORDER BY p.created_at DESC"
        )
        # No vector → rrf_score maps to the ts_rank so the service still has
        # a relevance number to surface. matched_vector is always false here.
        return text(
            f"""
            SELECT
                p.id,
                p.place_name,
                p.place_type,
                p.subcategory,
                p.tags,
                p.attributes,
                p.source_url,
                p.source,
                p.provider_id,
                p.created_at,
                ts_rank(p.search_vector, plainto_tsquery('english', :query_text)) AS rrf_score,
                FALSE AS matched_vector,
                TRUE AS matched_text
            FROM places p
            WHERE {where_sql}
              AND p.search_vector @@ plainto_tsquery('english', :query_text)
            {order}
            LIMIT :limit
            """
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    async def _total_count(self, where_sql: str, params: dict[str, Any]) -> int:
        """Count places matching the WHERE filter clauses only.

        Counts the full filter-scope match, ignoring query/RRF thresholds,
        distance cap, and LIMIT. Used for pagination total. Not the count
        of what was returned to the caller.
        """
        sql = text(f"SELECT COUNT(*) FROM places p WHERE {where_sql}")
        result = await self._session.scalar(sql, params)
        return int(result or 0)

    @staticmethod
    def _build_where_clause(
        user_id: str, filters: RecallFilters
    ) -> tuple[str, dict[str, Any]]:
        clauses = ["p.user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id}

        if filters.place_type is not None:
            clauses.append("p.place_type = :place_type")
            params["place_type"] = filters.place_type
        if filters.subcategory is not None:
            clauses.append("p.subcategory = :subcategory")
            params["subcategory"] = filters.subcategory
        if filters.source is not None:
            clauses.append("p.source = :source")
            params["source"] = filters.source
        if filters.created_after is not None:
            clauses.append("p.created_at >= :created_after")
            params["created_after"] = filters.created_after
        if filters.created_before is not None:
            clauses.append("p.created_at <= :created_before")
            params["created_before"] = filters.created_before
        # Attribute-level signals nest under filters.attributes to mirror
        # PlaceObject.attributes (ADR-056 + feature 027 pulled-forward M4).
        attrs = filters.attributes
        if attrs is not None:
            if attrs.cuisine is not None:
                clauses.append("p.attributes->>'cuisine' = :cuisine")
                params["cuisine"] = attrs.cuisine
            if attrs.price_hint is not None:
                clauses.append("p.attributes->>'price_hint' = :price_hint")
                params["price_hint"] = attrs.price_hint
            if attrs.ambiance is not None:
                clauses.append("p.attributes->>'ambiance' = :ambiance")
                params["ambiance"] = attrs.ambiance
            loc = attrs.location_context
            if loc is not None:
                if loc.neighborhood is not None:
                    clauses.append(
                        "p.attributes->'location_context'->>'neighborhood' = :neighborhood"
                    )
                    params["neighborhood"] = loc.neighborhood
                if loc.city is not None:
                    clauses.append("p.attributes->'location_context'->>'city' = :city")
                    params["city"] = loc.city
                if loc.country is not None:
                    clauses.append(
                        "p.attributes->'location_context'->>'country' = :country"
                    )
                    params["country"] = loc.country
        if filters.tags_include is not None:
            clauses.append("p.tags @> :tags_include::jsonb")
            # sqlalchemy will serialize the list; we pass JSON via json.dumps
            params["tags_include"] = json.dumps(filters.tags_include)

        return " AND ".join(clauses), params

    @staticmethod
    def _match_reason_from_row(row: Any) -> str:
        matched_vector = bool(row.get("matched_vector"))
        matched_text = bool(row.get("matched_text"))
        if matched_vector and matched_text:
            return "semantic + keyword"
        if matched_vector:
            return "semantic"
        if matched_text:
            return "keyword"
        return "filter"

    @staticmethod
    def _row_to_place_object(row: Any) -> PlaceObject:
        attributes_raw = row.get("attributes")
        attributes = (
            PlaceAttributes.model_validate(attributes_raw)
            if attributes_raw
            else PlaceAttributes()
        )
        source_raw = row.get("source")
        source = PlaceSource(source_raw) if source_raw else None
        tags = list(row.get("tags") or [])
        created_at = row.get("created_at")
        return PlaceObject(
            place_id=row["id"],
            place_name=row["place_name"],
            place_type=PlaceType(row["place_type"]),
            subcategory=row.get("subcategory"),
            tags=tags,
            attributes=attributes,
            source_url=row.get("source_url"),
            source=source,
            provider_id=row.get("provider_id"),
            created_at=created_at if isinstance(created_at, datetime) else None,
        )


__all__ = ["RecallRepository", "SQLAlchemyRecallRepository"]
