"""Cross-reference test for the field-name contract across places_v2.

Three sources reference the same logical field set:
  * `EmbeddingService._build_text` — Pydantic attribute access on PlaceCore.
  * `HybridSearchRepo._filter_conditions` — SQLAlchemy column refs.
  * The Alembic migration's `search_vector` generated column — raw SQL.

The library itself doesn't carry a constant for these fields; the
canonical list lives here as a test fixture. If a field name
disappears from any of the three sources, this test fails before merge.
"""

from __future__ import annotations

from pathlib import Path

# Canonical field list — anchor for all three consumers.
_SEARCHABLE_FIELDS: frozenset[str] = frozenset(
    {
        "place_name",
        "place_name_aliases",
        "categories",
        "tags",
        "neighborhood",
        "city",
        "country",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_MIGRATION_PATH = (
    _REPO_ROOT
    / "alembic"
    / "versions"
    / "a4d2c1b9e8f3_places_v2_category_to_categories_array.py"
)
_EMBEDDING_SERVICE_PATH = (
    _REPO_ROOT
    / "src"
    / "kebi"
    / "core"
    / "places_v2"
    / "embedding_service.py"
)
_HYBRID_SEARCH_REPO_PATH = (
    _REPO_ROOT
    / "src"
    / "kebi"
    / "core"
    / "places_v2"
    / "hybrid_search_repo.py"
)


def _missing_in(path: Path) -> list[str]:
    source = path.read_text()
    return sorted(f for f in _SEARCHABLE_FIELDS if f not in source)


class TestSearchableFieldsCrossReference:
    def test_every_field_in_migration(self) -> None:
        missing = _missing_in(_MIGRATION_PATH)
        assert not missing, (
            f"Migration {_MIGRATION_PATH.name} doesn't reference: {missing}. "
            f"Add them to the search_vector generated column."
        )

    def test_every_field_in_embedding_service(self) -> None:
        missing = _missing_in(_EMBEDDING_SERVICE_PATH)
        assert not missing, (
            f"embedding_service.py doesn't reference: {missing}. "
            f"Add them to _build_text."
        )

    def test_every_field_in_hybrid_search_repo(self) -> None:
        missing = _missing_in(_HYBRID_SEARCH_REPO_PATH)
        assert not missing, (
            f"hybrid_search_repo.py doesn't reference: {missing}. "
            f"Add them to _filter_conditions or the Table column list."
        )
