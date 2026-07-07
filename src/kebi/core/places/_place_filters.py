"""Shared SQL helpers for the `places ⋈ user_places` join.

Both hybrid search (`hybrid_search_repo`) and the library browse path
(`user_places_repo.browse`) filter the same join by the same predicate and
map the same place columns back to a `PlaceCore`. This module is the single
home for that shared surface so the logic is written once:

  * canonical typed table refs (`_PlacesTable` / `_UserPlacesTable`),
  * `build_filter_conditions` — the WHERE clauses for a `SavedPlaceFilters`,
  * `row_to_place_core` — a joined row's place columns → `PlaceCore`.
"""

from __future__ import annotations

import json

from sqlalchemy import (
    Boolean,
    Column,
    ColumnElement,
    DateTime,
    Float,
    MetaData,
    RowMapping,
    String,
    Table,
    Text,
    cast,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

from ._place_utils import escape_like
from .models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceTag,
    SavedPlaceFilters,
)

# ---------------------------------------------------------------------------
# Canonical typed table references — used by every repo that joins these two
# tables. `search_vector` is declared on places for the FTS leg of hybrid
# search; browse ignores it.
# ---------------------------------------------------------------------------
_metadata = MetaData()

_PlacesTable = Table(
    "places",
    _metadata,
    Column("id", String),
    Column("provider_id", String),
    Column("place_name", String),
    Column("place_name_aliases", JSONB),
    Column("categories", ARRAY(String)),
    Column("tags", JSONB),
    Column("location", JSONB),
    Column("created_at", DateTime(timezone=True)),
    Column("refreshed_at", DateTime(timezone=True)),
    Column("search_vector", TSVECTOR),
)
_p = _PlacesTable.c

_UserPlacesTable = Table(
    "user_places",
    _metadata,
    Column("user_place_id", String),
    Column("user_id", String),
    Column("place_id", String),
    Column("approved", Boolean),
    Column("visited", Boolean),
    Column("liked", Boolean),
    Column("note", Text),
    Column("source", String),
    Column("source_ref", Text),
    Column("source_label", Text),
    Column("saved_at", DateTime(timezone=True)),
    Column("visited_at", DateTime(timezone=True)),
)
_up = _UserPlacesTable.c


# JSONB tag containment encodes the PlaceTag.value field name as a literal SQL
# string. If the Pydantic field is renamed the query silently matches zero
# rows — fail loudly at import instead (mirrors places_repo).
assert "value" in PlaceTag.model_fields, (
    "PlaceTag.value field renamed — update tag JSONB containment below"
)


def build_filter_conditions(
    filters: SavedPlaceFilters,
) -> list[ColumnElement[bool]]:
    """Build WHERE conditions from a `SavedPlaceFilters`.

    Place-side conditions reference `_p`; user-side reference `_up`. The
    caller prepends the `_up.user_id == <user_id>` scope; this emits only
    the optional filter set.
    """
    conditions: list[ColumnElement[bool]] = []

    # ---- place catalog ----
    if filters.categories:
        # Array overlap (OR semantics): a place matches if its categories
        # share any element with the filter set. Cast as ARRAY(Text) — the
        # column is text[]; varchar[] mismatches the && operator (ADR-087).
        conditions.append(
            _p.categories.op("&&")(
                cast([c.value for c in filters.categories], ARRAY(Text))
            )
        )

    if filters.tags:
        # AND semantics: every requested tag value must be present.
        for tag_val in filters.tags:
            conditions.append(
                _p.tags.op("@>")(cast(json.dumps([{"value": tag_val}]), JSONB))
            )

    if filters.city:
        conditions.append(
            _p.location["city"].astext.ilike(
                f"%{escape_like(filters.city)}%", escape="\\"
            )
        )

    if filters.neighborhood:
        conditions.append(
            _p.location["neighborhood"].astext.ilike(
                f"%{escape_like(filters.neighborhood)}%", escape="\\"
            )
        )

    if filters.country:
        conditions.append(_p.location["country"].astext == filters.country)

    if (
        filters.lat is not None
        and filters.lng is not None
        and filters.radius_m is not None
    ):
        geo_lat = cast(_p.location["lat"].astext, Float())
        geo_lng = cast(_p.location["lng"].astext, Float())
        query_box = func.earth_box(
            func.ll_to_earth(filters.lat, filters.lng), float(filters.radius_m)
        )
        conditions.extend(
            [
                _p.location.isnot(None),
                _p.location["lat"].astext.isnot(None),
                _p.location["lng"].astext.isnot(None),
                query_box.op("@>")(func.ll_to_earth(geo_lat, geo_lng)),
            ]
        )

    # ---- user_places ----
    if filters.source is not None:
        conditions.append(_up.source == filters.source.value)

    if filters.visited is not None:
        conditions.append(_up.visited == filters.visited)

    if filters.liked is not None:
        conditions.append(_up.liked == filters.liked)

    if filters.approved is not None:
        conditions.append(_up.approved == filters.approved)

    if filters.saved_after is not None:
        conditions.append(_up.saved_at >= filters.saved_after)

    if filters.saved_before is not None:
        conditions.append(_up.saved_at <= filters.saved_before)

    return conditions


def row_to_place_core(row: RowMapping) -> PlaceCore:
    """Map the place columns of a joined row to a `PlaceCore`.

    Reads only the `places` columns (`id`, `provider_id`, `place_name`,
    `place_name_aliases`, `categories`, `tags`, `location`, `created_at`,
    `refreshed_at`); ignores any user_places / score columns alongside them.
    """
    tags = [PlaceTag.model_validate(t) for t in (row.get("tags") or [])]
    aliases = [
        PlaceNameAlias.model_validate(a) for a in (row.get("place_name_aliases") or [])
    ]
    loc_raw = row.get("location")
    location = LocationContext.model_validate(loc_raw) if loc_raw else None
    return PlaceCore(
        id=row.get("id"),
        provider_id=row.get("provider_id"),
        place_name=row["place_name"],
        place_name_aliases=aliases,
        categories=[PlaceCategory(c) for c in (row.get("categories") or [])],
        tags=tags,
        location=location,
        created_at=row.get("created_at"),
        refreshed_at=row.get("refreshed_at"),
    )
