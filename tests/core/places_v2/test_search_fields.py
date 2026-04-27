"""Tests for the SEARCHABLE_FIELDS contract.

Two layers:

  * Module-level assertions in `embedding_service.py` and
    `hybrid_search_repo.py` already fail-fast at import time if their
    private field sets drift from `SEARCHABLE_FIELDS`. These tests
    re-pin those sets here so the failure mode is also a unit test
    (not just an import error during app boot).
  * The Alembic migration is an SQL file; we substring-check that
    every `SEARCHABLE_FIELDS` name appears in the migration source —
    catches "added a field, forgot the migration" before it ships.
"""

from __future__ import annotations

from pathlib import Path

from totoro_ai.core.places_v2 import SEARCHABLE_FIELDS
from totoro_ai.core.places_v2.embedding_service import _EMBED_FIELDS
from totoro_ai.core.places_v2.hybrid_search_repo import (
    _FILTER_FIELDS,
    _FTS_ONLY_FIELDS,
)

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "e9f0a1b2c3d4_add_place_embeddings_v2_and_v2_fts.py"
)


class TestEmbeddingServiceContract:
    def test_embed_fields_match_searchable_fields(self) -> None:
        # The module-level assert in embedding_service.py would have
        # already failed at import; this just makes the contract
        # explicit in the test surface.
        assert _EMBED_FIELDS == SEARCHABLE_FIELDS


class TestHybridRepoContract:
    def test_filter_and_fts_only_partition_searchable_fields(self) -> None:
        assert _FILTER_FIELDS.isdisjoint(_FTS_ONLY_FIELDS)
        assert _FILTER_FIELDS | _FTS_ONLY_FIELDS == SEARCHABLE_FIELDS

    def test_filter_fields_is_strict_subset(self) -> None:
        # Sanity: filterable is a proper subset, since name + aliases
        # are search-only.
        assert _FILTER_FIELDS < SEARCHABLE_FIELDS


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
