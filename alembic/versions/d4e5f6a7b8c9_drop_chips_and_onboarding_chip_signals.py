"""drop taste_model.chips and chip/onboarding interaction types (ADR-076)

Removes the chips feature and the onboarding/chip-confirm signal tier:

- Deletes any interactions rows of type onboarding_confirm,
  onboarding_dismiss, chip_confirm (never consumed by signal aggregation,
  and they would block the enum recast).
- Rebuilds the interactiontype enum down to save/accepted/rejected.
- Drops the taste_model.chips JSONB column.

Note on downgrade: schema is restored (chips column re-added, the three
enum values re-added) but row data is NOT recovered — consistent with
the a7c3d2e9f4b1 migration note. PostgreSQL cannot drop an enum value in
place, hence the full type rebuild on upgrade.

Revision ID: d4e5f6a7b8c9
Revises: n030f1a2b3c4
Create Date: 2026-05-18 00:00:00.000000
"""
from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "n030f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Purge rows whose type is being removed (safe — these were never
    #    consumed by signal aggregation).
    #    Compare `type::text`, not the enum directly: on a fresh DB the whole
    #    migration chain replays in one transaction, and the enum value
    #    'chip_confirm' was ADDED by an earlier migration in that same
    #    transaction — Postgres forbids using a newly-added enum value before
    #    commit ("unsafe use of new value"). Casting to text compares plain
    #    string literals and sidesteps that check.
    op.execute(
        "DELETE FROM interactions "
        "WHERE type::text IN "
        "('onboarding_confirm','onboarding_dismiss','chip_confirm')"
    )

    # 2. Rebuild interactiontype enum without the removed values.
    op.execute("ALTER TABLE interactions ALTER COLUMN type DROP DEFAULT")
    op.execute("ALTER TYPE interactiontype RENAME TO interactiontype_old")
    op.execute(
        "CREATE TYPE interactiontype AS ENUM ('save','accepted','rejected')"
    )
    op.execute(
        "ALTER TABLE interactions "
        "ALTER COLUMN type TYPE interactiontype "
        "USING type::text::interactiontype"
    )
    op.execute("DROP TYPE interactiontype_old")

    # 3. Drop the chips column.
    op.drop_column("taste_model", "chips")


def downgrade() -> None:
    op.add_column(
        "taste_model",
        sa.Column(
            "chips",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Re-add the enum values (data is not restored — see module docstring).
    op.execute(
        "ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'onboarding_confirm'"
    )
    op.execute(
        "ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'onboarding_dismiss'"
    )
    op.execute(
        "ALTER TYPE interactiontype ADD VALUE IF NOT EXISTS 'chip_confirm'"
    )
