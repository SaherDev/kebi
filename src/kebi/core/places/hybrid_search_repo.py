"""HybridSearchRepo — vector + FTS RRF retrieval, two modes.

  * **Scoped** (`user_id` given) — the agent's recall path. The
    `filtered` CTE joins `user_places` with `places`, applies
    `user_id` + filters, and uses `DISTINCT ON (place_id) ORDER BY
    place_id, saved_at DESC` so a place saved twice by the same user
    collapses to one hit. Emits hits with `user_data` populated.
  * **Unscoped** (`user_id is None`) — global catalog search across
    every place anyone has saved. The `filtered` CTE selects
    `places` directly with no `user_places` JOIN; user-side
    filters (visited/liked/approved/saved_*) are rejected at the
    door because they don't make sense without a user. Emits hits
    with `user_data=None`.

One SQL round-trip composed via the SQLAlchemy expression API:

  1. `filtered` CTE: places ⋈ user_places, scoped by user_id and
     filtered (category, tags, geo, visited/liked/approved, saved_at
     range). Carries user_places columns through so downstream stages
     don't need to re-join.
  2. `vec` CTE: kNN over place_embeddings.vector (cosine, HNSW),
     restricted to filtered place ids. Top candidate_limit by rank.
  3. `txt` CTE: full-text on places.search_vector via
     `websearch_to_tsquery('simple_unaccent', :q)` with `ts_rank_cd`
     respecting per-field weights (A/B/C). Top candidate_limit.
  4. `fused`: FULL OUTER JOIN on place_id, RRF score
     `1/(k + v_rank) + 1/(k + t_rank)` (NULL ranks contribute 0).
  5. Final SELECT: re-joins `places` for PlaceCore columns and
     `filtered` for the user_places columns, ordered by rrf_score.

Repo does not embed — caller passes the query vector. Service owns the
embedder.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]
from sqlalchemy import (
    Column,
    ColumnElement,
    DateTime,
    MetaData,
    RowMapping,
    String,
    Table,
    and_,
    func,
    null,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from ._place_filters import (
    _p,
    _PlacesTable,
    _up,
    _UserPlacesTable,
    build_filter_conditions,
    place_core_columns,
    row_to_place_core,
)
from .embeddings_repo import EMBEDDING_DIMENSIONS
from .models import (
    HybridSearchFilters,
    HybridSearchHit,
    PlaceSource,
    UserPlace,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local Table reference — only the embeddings table is search-specific. The
# places ⋈ user_places tables and their filter/row helpers are shared via
# `_place_filters` (imported above) so the join logic lives in one place.
# ---------------------------------------------------------------------------

_metadata = MetaData()

_PlaceEmbeddingsTable = Table(
    "place_embeddings",
    _metadata,
    Column("id", String),
    Column("place_id", String),
    Column("vector", Vector(EMBEDDING_DIMENSIONS)),
    Column("model_name", String),
    Column("text_hash", String),
    Column("created_at", DateTime(timezone=True)),
)
_e = _PlaceEmbeddingsTable.c


_TS_CONFIG = "simple_unaccent"  # custom config from the FTS migration


class HybridSearchRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(
        self,
        user_id: str | None,
        query: str,
        query_vector: list[float],
        filters: HybridSearchFilters | None = None,
        limit: int = 20,
        rrf_k: int = 60,
        candidate_multiplier: int = 4,
    ) -> list[HybridSearchHit]:
        filters = filters or HybridSearchFilters()
        candidate_limit = limit * candidate_multiplier

        if user_id is None:
            _reject_user_side_filters(filters)
            filtered = self._build_unscoped_filtered_cte(filters)
        else:
            filtered = self._build_scoped_filtered_cte(user_id, filters)

        # ---- vec CTE -----------------------------------------------------
        cosine_dist = _e.vector.cosine_distance(query_vector)
        vec = (
            select(
                _e.place_id.label("place_id"),
                func.row_number().over(order_by=cosine_dist).label("rank"),
            )
            .select_from(
                _PlaceEmbeddingsTable.join(filtered, filtered.c.place_id == _e.place_id)
            )
            .order_by(cosine_dist)
            .limit(candidate_limit)
            .cte("vec")
        )

        # ---- txt CTE -----------------------------------------------------
        # PG implicitly casts the first arg to regconfig at runtime, so
        # passing the config name as a plain string param is fine.
        tsq = func.websearch_to_tsquery(_TS_CONFIG, query)
        text_rank = func.ts_rank_cd(_p.search_vector, tsq)
        txt = (
            select(
                _p.id.label("place_id"),
                func.row_number().over(order_by=text_rank.desc()).label("rank"),
            )
            .select_from(_PlacesTable.join(filtered, filtered.c.place_id == _p.id))
            .where(_p.search_vector.op("@@")(tsq))
            .order_by(text_rank.desc())
            .limit(candidate_limit)
            .cte("txt")
        )

        # ---- fused (FULL OUTER JOIN + RRF) -------------------------------
        rrf_score = (
            func.coalesce(1.0 / (rrf_k + vec.c.rank), 0)
            + func.coalesce(1.0 / (rrf_k + txt.c.rank), 0)
        ).label("rrf_score")
        fused = (
            select(
                func.coalesce(vec.c.place_id, txt.c.place_id).label("place_id"),
                rrf_score,
                vec.c.rank.label("vector_rank"),
                txt.c.rank.label("text_rank"),
            )
            .select_from(
                vec.outerjoin(txt, vec.c.place_id == txt.c.place_id, full=True)
            )
            .cte("fused")
        )

        # ---- final SELECT — PlaceCore columns + UserPlace columns + scores
        stmt = (
            select(
                *place_core_columns(),
                filtered.c.user_place_id,
                filtered.c.user_id,
                filtered.c.approved,
                filtered.c.visited,
                filtered.c.liked,
                filtered.c.note,
                filtered.c.source,
                filtered.c.source_ref,
                filtered.c.saved_at,
                filtered.c.visited_at,
                fused.c.rrf_score,
                fused.c.vector_rank,
                fused.c.text_rank,
            )
            .select_from(
                fused.join(_PlacesTable, _p.id == fused.c.place_id).join(
                    filtered, filtered.c.place_id == fused.c.place_id
                )
            )
            .order_by(fused.c.rrf_score.desc())
            .limit(limit)
        )

        try:
            result = await self._session.execute(stmt)
        except Exception as exc:
            logger.error(
                "hybrid_search.failed",
                extra={
                    "error": str(exc),
                    "user_id": user_id,
                    "query": query,
                },
            )
            raise

        return [_row_to_hit(row._mapping) for row in result]

    # ------------------------------------------------------------------
    # CTE builders — one per mode
    # ------------------------------------------------------------------

    @staticmethod
    def _build_scoped_filtered_cte(user_id: str, filters: HybridSearchFilters) -> Any:
        """places ⋈ user_places, scoped + deduped on place_id."""
        conditions: list[ColumnElement[bool]] = [
            _up.user_id == user_id,
            *build_filter_conditions(filters),
        ]
        return (
            select(
                _p.id.label("place_id"),
                _up.user_place_id,
                _up.user_id,
                _up.approved,
                _up.visited,
                _up.liked,
                _up.note,
                _up.source,
                _up.source_ref,
                _up.saved_at,
                _up.visited_at,
            )
            .distinct(_p.id)  # DISTINCT ON (place_id) — collapse duplicates
            .select_from(_PlacesTable.join(_UserPlacesTable, _up.place_id == _p.id))
            .where(and_(*conditions))
            .order_by(_p.id, _up.saved_at.desc())  # keep most recent save
            .cte("filtered")
        )

    @staticmethod
    def _build_unscoped_filtered_cte(filters: HybridSearchFilters) -> Any:
        """places only — no user scoping, user_places columns NULL.

        Padding the user_places columns with typed NULLs keeps the
        downstream final SELECT and row mapper identical to the scoped
        path; `_row_to_hit` checks `user_place_id is None` to decide
        whether to emit a UserPlace.
        """
        return (
            select(
                _p.id.label("place_id"),
                null().label("user_place_id"),
                null().label("user_id"),
                null().label("approved"),
                null().label("visited"),
                null().label("liked"),
                null().label("note"),
                null().label("source"),
                null().label("source_ref"),
                null().label("saved_at"),
                null().label("visited_at"),
            )
            .where(and_(*build_filter_conditions(filters)))
            .cte("filtered")
        )


# ---------------------------------------------------------------------------
# Filter conditions — typed columns from both tables
# ---------------------------------------------------------------------------


_USER_SIDE_FILTER_FIELDS = (
    "source",
    "visited",
    "liked",
    "approved",
    "saved_after",
    "saved_before",
)


def _reject_user_side_filters(filters: HybridSearchFilters) -> None:
    """User-side filters require user_id — fail loudly when unscoped.

    Quietly ignoring them would surface as confusing empty results
    ("why didn't visited=True filter anything?"). Raise at the door.
    """
    set_fields = [
        f for f in _USER_SIDE_FILTER_FIELDS if getattr(filters, f) is not None
    ]
    if set_fields:
        raise ValueError(
            f"user-side filters {set_fields} require a user_id "
            f"(unscoped search has no user_places rows to filter)"
        )


# ---------------------------------------------------------------------------
# Row → HybridSearchHit
# ---------------------------------------------------------------------------


def _row_to_hit(row: RowMapping) -> HybridSearchHit:
    place = row_to_place_core(row)

    # user_data is None on rows produced by the unscoped CTE (it pads
    # the user_places columns with NULL). user_place_id is the cheapest
    # discriminator since it's NOT NULL when a real user_places row was
    # joined in.
    if row.get("user_place_id") is not None:
        user_data: UserPlace | None = UserPlace(
            user_place_id=row["user_place_id"],
            user_id=row["user_id"],
            place_id=row["id"],
            approved=bool(row.get("approved", True)),
            visited=bool(row.get("visited", False)),
            liked=row.get("liked"),
            note=row.get("note"),
            source=PlaceSource(row["source"]),
            source_ref=row.get("source_ref"),
            saved_at=row["saved_at"],
            visited_at=_to_datetime(row.get("visited_at")),
        )
    else:
        user_data = None

    v_rank = row.get("vector_rank")
    t_rank = row.get("text_rank")
    return HybridSearchHit(
        place=place,
        user_data=user_data,
        rrf_score=float(row["rrf_score"]),
        vector_rank=int(v_rank) if v_rank is not None else None,
        text_rank=int(t_rank) if t_rank is not None else None,
    )


def _to_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
