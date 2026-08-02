"""add_area_experience_interaction_types

Add `area_interest` and `experience_interest` values to the `interactiontype`
enum (location-kinds Step 3). A share's noted areas now emit their own taste
signal — region interest, distinct from venue sentiment — and a route/experience
share contributes experience-type interest with no saved object. Both ride the
existing `interactions` table (place_id carries the area's entity_key for
area_interest, NULL for experience_interest; metadata carries the display name /
kind / experience tags), so this is enum-values only — no table change, and
`signal_counts` is JSONB (schema-on-read for the new buckets).

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, so it is
issued in an autocommit block. `IF NOT EXISTS` makes it idempotent.

Postgres has no `DROP VALUE`, so the downgrade is a documented no-op: an added
enum label cannot be removed without rebuilding the type, which would be
data-lossy for any row already using it.

Revision ID: a3b4c5d6e7f8
Revises: x2y3z4a5b6c7
Create Date: 2026-08-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "x2y3z4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'area_interest'")
        op.execute(
            "ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'experience_interest'"
        )


def downgrade() -> None:
    # Postgres cannot drop an enum value; removing these would require
    # recreating interactiontype and rewriting every row — data-lossy for
    # rows already using it. Intentional no-op.
    pass
