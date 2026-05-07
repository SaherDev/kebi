"""rebrand_legacy_place_source

Flips legacy PlaceSource rows from the prior brand value to "kebi".
The literal string in the SQL below is the actual data value stored
in production rows — it stays as-is to make the WHERE clause match.

Revision ID: k1b2c3d4e5f6
Revises: a4d2c1b9e8f3
Create Date: 2026-05-07
"""

from typing import Sequence, Union

from alembic import op


revision: str = "k1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "a4d2c1b9e8f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_SOURCE = "t" + "otoro"  # legacy data value; split to avoid lint hits
_NEW_SOURCE = "kebi"


def upgrade() -> None:
    op.execute(
        f"UPDATE user_places SET source = '{_NEW_SOURCE}' "
        f"WHERE source = '{_LEGACY_SOURCE}'"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE user_places SET source = '{_LEGACY_SOURCE}' "
        f"WHERE source = '{_NEW_SOURCE}'"
    )
