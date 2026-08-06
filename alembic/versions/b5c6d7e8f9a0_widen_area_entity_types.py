"""widen_area_entity_types

Add `region`, `natural_feature`, and `street` to the `area_entity_type` enum
(location-kinds Step 6). An area can now be an answer at whatever granularity
the question asks, and a named natural feature or street is stored as the
geography it is rather than passing as a venue.

Enum-values only — no table change. `area_entities` already carries `bbox`,
`place_type` and `parent_key`, which is everything the new kinds need.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block, so it is
issued in an autocommit block. `IF NOT EXISTS` makes it idempotent.

Postgres has no `DROP VALUE`, so the downgrade is a documented no-op: an added
enum label cannot be removed without rebuilding the type, which would be
data-lossy for any row already using it.

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
Create Date: 2026-08-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in ("region", "natural_feature", "street"):
            op.execute(
                f"ALTER TYPE area_entity_type ADD VALUE IF NOT EXISTS '{value}'"
            )


def downgrade() -> None:
    # Postgres cannot drop an enum value; removing these would require
    # recreating area_entity_type and rewriting every row — data-lossy for
    # rows already using it. Intentional no-op.
    pass
