"""purge interactions orphaned from the places_v2 catalog (ADR-077)

The taste model now resolves behavioral signals against the places_v2
catalog. Pre-cutover interactions reference the retired legacy place
identity space and cannot be mapped forward, so regeneration would skip
them anyway. This deletes those orphan rows up front so signal counts
and the stale-regen guard reflect only resolvable signal.

Note on downgrade: schema is unchanged; deleted rows are NOT restored —
consistent with the a7c3d2e9f4b1 / d4e5f6a7b8c9 migration notes.

Revision ID: c7d8e9f0a1b2
Revises: d4e5f6a7b8c9
Create Date: 2026-05-18 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop interactions whose place_id does not resolve to a places_v2 row
    # (legacy pre-cutover ids). No schema / enum / FK change.
    op.execute(
        "DELETE FROM interactions i "
        "WHERE i.place_id IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM places_v2 p WHERE p.id = i.place_id)"
    )


def downgrade() -> None:
    # No-op: deleted rows are not recoverable (see module docstring).
    pass
