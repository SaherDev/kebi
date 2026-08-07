"""Add the areas table — the area screen's stored global half (ADR-153).

Chat has linked areas since ADR-136, but a tapped area had nowhere to land:
a venue link resolves to its catalog row, an area link resolved to nothing.
This table gives a geo key a row the first time someone opens it — profile
prose, chips, breadcrumb names, notable children — generated once by the
area profiler and shared by every user. Row presence doubles as the
"already profiled" marker, so no separate flag column exists.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "z4a5b6c7d8e9"
down_revision = "y3z4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "areas",
        sa.Column("geo_key", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "best_for", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "breadcrumb",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "notable_sub_areas",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "profiled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("areas")
