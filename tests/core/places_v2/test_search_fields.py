"""Tests for the SEARCHABLE_FIELDS contract.

The set is injected into both `EmbeddingService` and `HybridSearchRepo`
constructors and gates each field-specific block. Tests verify:

  * Default `EmbeddingService(...)._build_text` includes every
    SEARCHABLE_FIELDS field when the corresponding PlaceCore value is
    populated.
  * Passing a smaller `fields` set disables those blocks.
  * The Alembic migration source references every name (substring
    check — the migration is hardcoded SQL and can't import).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.places_v2 import SEARCHABLE_FIELDS
from totoro_ai.core.places_v2.embedding_service import EmbeddingService
from totoro_ai.core.places_v2.models import (
    HybridSearchFilters,
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceTag,
)
from totoro_ai.core.places_v2.tags import (
    AtmosphereTag,
    CuisineTag,
    TagType,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "e9f0a1b2c3d4_add_place_embeddings_v2_and_v2_fts.py"
)


def _full_core() -> PlaceCore:
    """A PlaceCore with every searchable field populated."""
    return PlaceCore(
        id="p1",
        provider_id="google:abc",
        place_name="Trattoria Roma",
        place_name_aliases=[
            PlaceNameAlias(value="Roma Shibuya", source="tiktok"),
        ],
        category=PlaceCategory.restaurant,
        tags=[
            PlaceTag(
                type=TagType.cuisine,
                value=CuisineTag.italian,
                source="google",
            ),
            PlaceTag(
                type=TagType.atmosphere,
                value=AtmosphereTag.cozy,
                source="llm",
            ),
        ],
        location=LocationContext(
            neighborhood="Shibuya",
            city="Tokyo",
            country="Japan",
        ),
    )


def _service(fields: frozenset[str] | None = None) -> EmbeddingService:
    repo = MagicMock()
    repo.upsert_embeddings = AsyncMock()
    repo.get_signatures_by_place_ids = AsyncMock(return_value={})
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[0.1] * 8])
    if fields is None:
        return EmbeddingService(repo, embedder, "voyage-4-lite")
    return EmbeddingService(repo, embedder, "voyage-4-lite", fields=fields)


# ---------------------------------------------------------------------------
# Default field set
# ---------------------------------------------------------------------------


class TestDefaultFieldSet:
    def test_searchable_fields_is_a_frozenset(self) -> None:
        assert isinstance(SEARCHABLE_FIELDS, frozenset)
        assert len(SEARCHABLE_FIELDS) >= 7

    def test_default_build_text_includes_every_field(self) -> None:
        service = _service()
        text = service._build_text(_full_core())
        assert "Trattoria Roma" in text       # place_name
        assert "Roma Shibuya" in text         # alias
        assert "restaurant" in text.lower()    # category
        assert "italian" in text.lower()       # tag value
        assert "Shibuya" in text               # neighborhood
        assert "Tokyo" in text                 # city
        assert "Japan" in text                 # country


# ---------------------------------------------------------------------------
# Field gating — passing a subset disables blocks
# ---------------------------------------------------------------------------


class TestEmbeddingFieldGating:
    def test_dropping_place_name_omits_name_line(self) -> None:
        service = _service(fields=SEARCHABLE_FIELDS - {"place_name"})
        text = service._build_text(_full_core())
        assert "Trattoria Roma" not in text
        # Other fields still present
        assert "italian" in text.lower()

    def test_dropping_aliases_omits_alias_line(self) -> None:
        service = _service(fields=SEARCHABLE_FIELDS - {"place_name_aliases"})
        text = service._build_text(_full_core())
        assert "Roma Shibuya" not in text

    def test_dropping_category_omits_category_line(self) -> None:
        service = _service(fields=SEARCHABLE_FIELDS - {"category"})
        text = service._build_text(_full_core())
        assert "Category" not in text

    def test_dropping_tags_omits_tag_lines(self) -> None:
        service = _service(fields=SEARCHABLE_FIELDS - {"tags"})
        text = service._build_text(_full_core())
        assert "italian" not in text.lower()
        assert "cozy" not in text.lower()

    def test_dropping_only_city_keeps_neighborhood_and_country(self) -> None:
        service = _service(fields=SEARCHABLE_FIELDS - {"city"})
        text = service._build_text(_full_core())
        assert "Shibuya" in text
        assert "Japan" in text
        assert "Tokyo" not in text

    def test_empty_field_set_yields_empty_text(self) -> None:
        service = _service(fields=frozenset())
        text = service._build_text(_full_core())
        assert text == ""


# ---------------------------------------------------------------------------
# Hybrid repo gating — verified by the existing repo tests; here we only
# check that passing a subset to the constructor doesn't blow up and
# that the place-side filter for an excluded field is dropped.
# ---------------------------------------------------------------------------


class TestRepoFieldGating:
    async def test_dropping_category_disables_its_filter(self) -> None:
        from sqlalchemy.dialects import postgresql as pg_dialect

        from totoro_ai.core.places_v2.hybrid_search_repo import HybridSearchRepo

        session = MagicMock()
        result = MagicMock()
        result.__iter__ = MagicMock(return_value=iter([]))
        session.execute = AsyncMock(return_value=result)

        repo = HybridSearchRepo(session, fields=SEARCHABLE_FIELDS - {"category"})
        await repo.search(
            "u1",
            "italian",
            [0.1] * 1024,
            filters=HybridSearchFilters(category=PlaceCategory.cafe),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(
            stmt.compile(
                dialect=pg_dialect.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        # 'category' may still appear as a column name in the SELECT,
        # but the filter literal 'cafe' from PlaceCategory.cafe should be
        # absent from the WHERE clause when the field is gated out.
        assert "= 'cafe'" not in sql

    async def test_dropping_geo_does_nothing(self) -> None:
        # geo isn't a per-field filter; it stays on regardless of the
        # injected fields.
        from sqlalchemy.dialects import postgresql as pg_dialect

        from totoro_ai.core.places_v2.hybrid_search_repo import HybridSearchRepo

        session = MagicMock()
        result = MagicMock()
        result.__iter__ = MagicMock(return_value=iter([]))
        session.execute = AsyncMock(return_value=result)

        repo = HybridSearchRepo(session, fields=frozenset())
        await repo.search(
            "u1",
            "italian",
            [0.1] * 1024,
            filters=HybridSearchFilters(lat=35.6, lng=139.7, radius_m=500),
        )
        stmt = session.execute.call_args.args[0]
        sql = str(
            stmt.compile(
                dialect=pg_dialect.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).lower()
        # Geo is unconditional — earth_box must still be present.
        assert "earth_box" in sql


# ---------------------------------------------------------------------------
# Migration cross-reference
# ---------------------------------------------------------------------------



class TestMigrationContract:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_PATH.is_file(), (
            f"Migration not found at {_MIGRATION_PATH}"
        )

    def test_every_searchable_field_appears_in_migration(self) -> None:
        source = _MIGRATION_PATH.read_text()
        missing = [f for f in SEARCHABLE_FIELDS if f not in source]
        assert not missing, (
            f"Migration {_MIGRATION_PATH.name} is missing references to "
            f"SEARCHABLE_FIELDS: {missing}. Add them to the search_vector "
            f"generated column."
        )
