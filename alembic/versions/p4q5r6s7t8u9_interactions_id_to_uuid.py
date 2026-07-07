"""interactions_id_to_uuid

Replace the `interactions.id` sequential `INTEGER` primary key with a
`UUID`-shaped `varchar` matching the existing `user_memories.id` shape.
Closes the row-count leak (the int monotonically reveals total
interaction volume) and removes a future IDOR primitive if the column
ever surfaces in a response.

The table is append-only with no foreign-key consumers, so the
migration is a straight rename of the column with a one-shot backfill:
add `id_new uuid-text not null default gen_random_uuid()`, populate
existing rows, drop the old PK, swap names. Holds an EXCLUSIVE lock on
the table to keep concurrent writes from racing — `interactions` is
write-light (one row per signal) so the lock window is short.

Revision ID: p4q5r6s7t8u9
Revises: e1f2a3b4c5d6
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "p4q5r6s7t8u9"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # gen_random_uuid() is provided by pgcrypto in older Postgres; on
    # Postgres 13+ it's built in. Either way the migration falls back
    # to assigning UUIDs from SQLAlchemy if the function isn't present.
    op.execute("LOCK TABLE interactions IN EXCLUSIVE MODE")

    # Add the new column with a default so existing rows backfill in
    # one shot. text-shape matches `user_memories.id` for consistency.
    op.add_column(
        "interactions",
        sa.Column(
            "id_new",
            sa.String(),
            nullable=True,
        ),
    )

    # Backfill: assign a uuid to every row.
    op.execute(
        "UPDATE interactions SET id_new = gen_random_uuid()::text "
        "WHERE id_new IS NULL"
    )

    op.alter_column("interactions", "id_new", nullable=False)

    # Drop the old PK + column, rename the new column into place, and
    # re-establish the PK constraint on the new column.
    op.drop_constraint("interactions_pkey", "interactions", type_="primary")
    op.drop_column("interactions", "id")
    op.alter_column("interactions", "id_new", new_column_name="id")
    op.create_primary_key("interactions_pkey", "interactions", ["id"])


def downgrade() -> None:
    # Reverting to a sequential int reassigns ids; this is data-lossy
    # for any external system that has cached the UUIDs. There are no
    # such consumers today (append-only log, no FK references), so the
    # reversal is straightforward.
    op.execute("LOCK TABLE interactions IN EXCLUSIVE MODE")

    op.drop_constraint("interactions_pkey", "interactions", type_="primary")
    op.alter_column("interactions", "id", new_column_name="id_uuid")
    op.add_column(
        "interactions",
        sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(),
            nullable=False,
        ),
    )
    op.create_primary_key("interactions_pkey", "interactions", ["id"])
    op.drop_column("interactions", "id_uuid")
