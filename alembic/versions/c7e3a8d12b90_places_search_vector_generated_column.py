"""places_search_vector_generated_column

Feature 019 / T011b: replace the inline-expression FTS index with a
`GENERATED ALWAYS AS ... STORED` `search_vector` column. Hybrid-search
queries can then filter and rank on `p.search_vector` directly without
recomputing the tsvector per row.

# ⚠️ COUPLING: The fields in this generated column expression must exactly match
# config/app.yaml embeddings.description_fields (minus tags, good_for, dietary,
# place_type — these are intentionally excluded from FTS; see ADR-055).
# Changing description_fields requires:
#   1. A new Alembic migration to update this generated column expression
#   2. A full re-embedding of all saved places
# Both steps must happen together or retrieval quality degrades silently.

Revision ID: c7e3a8d12b90
Revises: 9a1c7b54e2f0
Create Date: 2026-04-15 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e3a8d12b90"
down_revision: str | None = "9a1c7b54e2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the inline-expression FTS index created by the schema migration.
    op.execute("DROP INDEX IF EXISTS places_fts_idx")

    # Add the generated tsvector column. The expression must stay in sync
    # with config/app.yaml embeddings.description_fields per ADR-055 — the
    # startup validator in kebi.api.main checks the alignment on boot.
    op.execute(
        """
        ALTER TABLE places ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(place_name, '') || ' ' ||
                coalesce(subcategory, '') || ' ' ||
                coalesce(attributes->>'cuisine', '') || ' ' ||
                coalesce(attributes->>'ambiance', '') || ' ' ||
                coalesce(attributes->>'price_hint', '') || ' ' ||
                coalesce(attributes->'location_context'->>'neighborhood', '') || ' ' ||
                coalesce(attributes->'location_context'->>'city', '') || ' ' ||
                coalesce(attributes->'location_context'->>'country', '')
            )
        ) STORED
        """
    )

    # Rebuild the GIN index, now over the stored column.
    op.execute("CREATE INDEX places_fts_idx ON places USING gin(search_vector)")


def downgrade() -> None:
    # Drop the GIN index and the generated column, then restore the
    # original inline-expression index from the schema migration.
    op.execute("DROP INDEX IF EXISTS places_fts_idx")
    op.execute("ALTER TABLE places DROP COLUMN IF EXISTS search_vector")
    op.execute(
        """
        CREATE INDEX places_fts_idx
          ON places
          USING gin(
            to_tsvector('english',
              coalesce(place_name, '') || ' ' || coalesce(subcategory, ''))
          )
        """
    )
