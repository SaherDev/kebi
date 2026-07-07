"""recover_user_places_and_embeddings_v2

Spec 030 Phase 6 follow-up. The places_v2 / user_places /
place_embeddings_v2 chain (d1e2f3a4b5c6 → e9f0a1b2c3d4 → ...) is
already in the alembic version chain and stamps as applied — but at
least one developer environment hit a state where alembic_version
records the migrations as run while `user_places` is missing from
the running Postgres. Likely cause: a previous version of
d1e2f3a4b5c6 didn't include `user_places` and was stamped, then
the migration was edited.

This migration recovers from that state. Every operation is
idempotent (IF NOT EXISTS or pg_class lookup) so it is safe to run
on a DB that already has the tables — it just no-ops.

Revision ID: n030f1a2b3c4
Revises: k1b2c3d4e5f6
Create Date: 2026-05-14 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "n030f1a2b3c4"
down_revision: str | tuple[str, ...] | None = "k1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Keep in sync with EMBEDDING_DIMENSIONS in
# src/kebi/core/places_v2/embeddings_repo.py.
EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    # Geo + vector + unaccent extensions — all CREATE EXTENSION IF NOT EXISTS.
    op.execute("CREATE EXTENSION IF NOT EXISTS cube")
    op.execute("CREATE EXTENSION IF NOT EXISTS earthdistance")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # ------------------------------------------------------------------
    # user_places — per (user, place) record. Mirrors d1e2f3a4b5c6 but
    # IF NOT EXISTS so it no-ops when the table is already there.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_places (
            user_place_id text PRIMARY KEY,
            user_id text NOT NULL,
            place_id text NOT NULL
                REFERENCES places_v2(id) ON DELETE CASCADE,
            approved boolean NOT NULL DEFAULT true,
            visited boolean NOT NULL DEFAULT false,
            liked boolean,
            note text,
            source text NOT NULL,
            source_url text,
            saved_at timestamptz NOT NULL,
            visited_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_places_user_id "
        "ON user_places (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_places_place_id "
        "ON user_places (place_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_places_user_saved "
        "ON user_places (user_id, saved_at)"
    )

    # ------------------------------------------------------------------
    # place_embeddings_v2 — one vector per place. Mirrors e9f0a1b2c3d4
    # body, idempotent.
    # ------------------------------------------------------------------
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS place_embeddings_v2 (
            id text PRIMARY KEY,
            place_id text NOT NULL UNIQUE
                REFERENCES places_v2(id) ON DELETE CASCADE,
            vector vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            model_name text NOT NULL,
            text_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # HNSW vector index — guarded with an explicit existence check
    # because CREATE INDEX IF NOT EXISTS combined with USING hnsw can
    # be picky on older pgvector versions; this form is bulletproof.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class
                WHERE relname = 'place_embeddings_v2_vector_hnsw_idx'
            ) THEN
                CREATE INDEX place_embeddings_v2_vector_hnsw_idx
                    ON place_embeddings_v2
                    USING hnsw (vector vector_cosine_ops);
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    # Pure no-op safety: this migration was a recovery and removing
    # the tables here would destroy data without recreating them
    # elsewhere. If a downgrade is genuinely needed, drop manually.
    pass
