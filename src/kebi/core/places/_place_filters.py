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
from typing import Any

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
    literal_column,
    or_,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

from kebi.core.knowledge.schemas import _slugify

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
    Column("icon", String),
    Column("location", JSONB),
    Column("created_at", DateTime(timezone=True)),
    Column("refreshed_at", DateTime(timezone=True)),
    # Derived on write from `location` — see `geo_key_for_location`.
    Column("geo_key", String),
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

# The free-text needle reads both JSONB arrays through the jsonpath `$[*].value`,
# which hardcodes the same field name on both models. A rename would silently
# stop matching tags or aliases — fail at import instead.
assert "value" in PlaceNameAlias.model_fields, (
    "PlaceNameAlias.value field renamed — update the `$[*].value` path below"
)


def _jsonb_values_ilike(col: Any, pattern: str) -> ColumnElement[bool]:
    """Match a needle against the `value` of every object in a JSONB array.

    `jsonb_path_query_array` projects just the values (`["cafe", "brunch"]`)
    before the ILIKE, so the match cannot land on a key name or a provenance
    field — searching "google" must not return every place whose tags happen
    to be Google-sourced. NULL columns yield NULL and simply don't match.
    """
    return cast(
        func.jsonb_path_query_array(col, literal_column("'$[*].value'")), Text
    ).ilike(pattern, escape="\\")


def _free_text_condition(needle: str) -> ColumnElement[bool]:
    """The `query` filter: one OR-group over everything a person might type.

    Name, alternative names, the place's area words (as stored *and* as the
    library names them), its country, tags and categories — matched as a
    case-insensitive substring so the predicate holds mid-word while the
    user is still typing. Deliberately *not* the
    `search_vector` FTS index: `websearch_to_tsquery` matches whole lexemes,
    so "cang" would find nothing until "canggu" is fully typed.

    Unindexed by nature (a leading wildcard can't use a b-tree), which is
    affordable only because every caller ANDs this with `user_id` — the scan
    is over one person's library, never the catalog.
    """
    pattern = f"%{escape_like(needle)}%"

    # The area as the *library* names it, not as the provider spelled it.
    # A section heading is derived from `geo_key`, and the key is a folded,
    # transliterated form of the raw location — so a place under the
    # "Bangkok" heading may store "Krung Thep Maha Nakhon", and one under
    # "Canggu" may store "Tibubeneng". Matching only the display strings
    # meant search denied places its own headings were promising: "no
    # matches for bangkok" directly above a Bangkok section holding ten.
    # The needle is slugified the same way the key was built, so "hoi an"
    # and "Hội An" both reach `hoi-an`, and the country prefix is stripped
    # so a two-letter needle cannot drag in a whole country.
    conditions = [
        _p.place_name.ilike(pattern, escape="\\"),
        _jsonb_values_ilike(_p.place_name_aliases, pattern),
        _p.location["city"].astext.ilike(pattern, escape="\\"),
        _p.location["neighborhood"].astext.ilike(pattern, escape="\\"),
        _p.location["country"].astext.ilike(pattern, escape="\\"),
        _jsonb_values_ilike(_p.tags, pattern),
        func.array_to_string(_p.categories, " ").ilike(pattern, escape="\\"),
    ]
    if slug := _slugify(needle):
        conditions.append(
            func.regexp_replace(_p.geo_key, "^[a-z]{2}/", "").ilike(
                f"%{escape_like(slug)}%", escape="\\"
            )
        )
    return or_(*conditions)


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
    # A blank or whitespace-only needle is "no search", not "match nothing".
    if filters.query and filters.query.strip():
        conditions.append(_free_text_condition(filters.query.strip()))

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

    if filters.area:
        # Prefix, not equality: an area contains its children, so `id/bali`
        # must return the saves keyed `id/bali/canggu`. The `/` guard stops
        # `id/bal` matching `id/bali` — a key segment is whole or it is a
        # different area.
        key = filters.area.strip("/")
        conditions.append(
            or_(
                _p.geo_key == key,
                _p.geo_key.like(f"{escape_like(key)}/%", escape="\\"),
            )
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
        icon=row.get("icon"),
        location=location,
        created_at=row.get("created_at"),
        refreshed_at=row.get("refreshed_at"),
    )
