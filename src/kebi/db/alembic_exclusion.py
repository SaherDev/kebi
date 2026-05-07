"""Shared helpers for Alembic autogenerate exclusion (feature 027 M3, FR-031).

Pulled out of `alembic/env.py` so the pure logic is importable and testable
without booting Alembic's context. `env.py` re-imports from here.
"""

from __future__ import annotations

# `langgraph-checkpoint-postgres` manages its own schema via
# AsyncPostgresSaver.setup(). Alembic must ignore these tables so
# `alembic check` does not flag them as drift.
LIBRARY_MANAGED_TABLES: frozenset[str] = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


def include_object(
    object: object, name: str, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Return False for library-owned checkpointer tables and their indexes."""
    if type_ == "table" and name in LIBRARY_MANAGED_TABLES:
        return False
    if type_ == "index":
        table = getattr(object, "table", None)
        if table is not None and getattr(table, "name", None) in LIBRARY_MANAGED_TABLES:
            return False
    return True
