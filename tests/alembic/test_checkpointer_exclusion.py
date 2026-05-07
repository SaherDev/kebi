"""Alembic include_object filter for library-owned checkpointer tables (FR-031)."""

from __future__ import annotations

from kebi.db.alembic_exclusion import (
    LIBRARY_MANAGED_TABLES,
    include_object,
)


def test_library_tables_excluded() -> None:
    for name in LIBRARY_MANAGED_TABLES:
        assert (
            include_object(
                object=None, name=name, type_="table", reflected=True, compare_to=None
            )
            is False
        )


def test_non_library_tables_pass_through() -> None:
    for name in ("places", "users", "user_memories", "recommendations"):
        assert (
            include_object(
                object=None, name=name, type_="table", reflected=True, compare_to=None
            )
            is True
        )


def test_checkpoint_tables_registered() -> None:
    """The three documented tables (plus migrations helper) are in the set."""
    for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert name in LIBRARY_MANAGED_TABLES


def test_index_on_library_table_excluded() -> None:
    """Indexes belonging to library-owned tables are also filtered out."""

    class FakeTable:
        name = "checkpoints"

    class FakeIndex:
        table = FakeTable()

    assert (
        include_object(
            object=FakeIndex(),
            name="checkpoints_thread_id_idx",
            type_="index",
            reflected=True,
            compare_to=None,
        )
        is False
    )
