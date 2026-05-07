"""add_place_embeddings_v2_and_v2_fts

Creates the `place_embeddings_v2` table (one vector per place_id) and
the `places_v2.search_vector` generated tsvector column, plus the
indexes needed for hybrid search:

  * HNSW index on `place_embeddings_v2.vector` (cosine ops) — kNN side
  * GIN index on `places_v2.search_vector` — full-text side

The full-text side uses a dedicated `simple_unaccent` text-search
config (simple tokenization + unaccent dictionary) so accented and
unaccented forms collide ("Café" matches "cafe"). Field weights:

  * A — place_name, place_name_aliases (a name match always wins)
  * B — category, tags                  (semantic content)
  * C — location.neighborhood/city/country  (context, not the target)

The query side MUST use the same `simple_unaccent` config when
building the tsquery, otherwise indexed lexemes won't line up with
search terms.

The generated tsvector pulls scalar fields directly and JSONB array
values via `jsonb_path_query_array($[*].value)`. Listing those JSONB
columns explicitly here keeps the generated expression IMMUTABLE
(required for STORED columns) and avoids tokenizing JSON keys.

Revision ID: e9f0a1b2c3d4
Revises: d1e2f3a4b5c6
Create Date: 2026-04-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | tuple[str, ...] | None = "c7a3f1d8e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Keep in sync with EMBEDDING_DIMENSIONS in
# src/kebi/core/places_v2/embeddings_repo.py.
EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    # ------------------------------------------------------------------
    # place_embeddings_v2 — one row per place_id, FK → places_v2 with
    # ON DELETE CASCADE so a wiped place takes its vector with it.
    # ------------------------------------------------------------------
    op.create_table(
        "place_embeddings_v2",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column(
            "place_id",
            sa.String,
            sa.ForeignKey("places_v2.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("vector", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("model_name", sa.String, nullable=False),
        sa.Column("text_hash", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # HNSW index on the vector column (cosine distance). Approximate kNN
    # but ~100x faster than a seq scan past ~100k rows. Built lazily —
    # writes don't block.
    op.execute(
        "CREATE INDEX place_embeddings_v2_vector_hnsw_idx "
        "ON place_embeddings_v2 USING hnsw (vector vector_cosine_ops)"
    )

    # ------------------------------------------------------------------
    # places_v2.search_vector — generated tsvector over the lexical
    # fields the recall path actually queries. 'simple' tokenization
    # (no stemming) keeps non-English place names intact; the unaccent
    # dictionary on top folds "Café" → "cafe" so accented forms match.
    #
    # Field weights via setweight():
    #   A — place_name, place_name_aliases  (direct name match wins)
    #   B — category, tags                   (semantic content)
    #   C — location.{neighborhood,city,country}  (context)
    #
    # JSONB arrays are flattened with jsonb_path_query_array($[*].value)
    # so only the value strings land in the tsvector — JSON keys like
    # "type" / "source" / "value" stay out of the index.
    #
    # Field list is paired with EmbeddingService._build_text in
    # core/places_v2/embedding_service.py. Add a field there → add it
    # here too, or FTS and vector recall surface different places.
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # Custom config = simple + unaccent. Naming it explicitly means
    # to_tsvector('simple_unaccent', ...) is IMMUTABLE for the column's
    # purposes (the regconfig OID is bound at parse time).
    op.execute(
        "CREATE TEXT SEARCH CONFIGURATION simple_unaccent (COPY = simple)"
    )
    op.execute(
        """
        ALTER TEXT SEARCH CONFIGURATION simple_unaccent
            ALTER MAPPING FOR hword, hword_part, word
            WITH unaccent, simple
        """
    )

    op.execute(
        """
        ALTER TABLE places_v2 ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('simple_unaccent',
                coalesce(place_name, '')), 'A') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(
                    jsonb_path_query_array(place_name_aliases, '$[*].value')::text,
                    ''
                )), 'A') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(category, '')), 'B') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(
                    jsonb_path_query_array(tags, '$[*].value')::text,
                    ''
                )), 'B') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(location->>'neighborhood', '')), 'C') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(location->>'city', '')), 'C') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(location->>'country', '')), 'C')
        ) STORED
        """
    )

    op.execute(
        "CREATE INDEX places_v2_fts_idx "
        "ON places_v2 USING gin(search_vector)"
    )

    # ------------------------------------------------------------------
    # places_v2.tags — GIN index for `tags @> '[{"value": ...}]'`
    # containment checks used by HybridSearchFilters.tags. Without this,
    # every tag filter does a sequential JSONB scan. jsonb_path_ops is
    # the narrow operator class — covers @> only, smaller and faster
    # than the default jsonb_ops which also indexes key existence (we
    # don't query for keys).
    # ------------------------------------------------------------------
    op.execute(
        "CREATE INDEX places_v2_tags_idx "
        "ON places_v2 USING gin(tags jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS places_v2_tags_idx")
    op.execute("DROP INDEX IF EXISTS places_v2_fts_idx")
    op.execute("ALTER TABLE places_v2 DROP COLUMN IF EXISTS search_vector")
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS simple_unaccent")
    # Leave the unaccent extension in place — it's harmless and other
    # consumers may rely on it.

    op.execute("DROP INDEX IF EXISTS place_embeddings_v2_vector_hnsw_idx")
    op.drop_table("place_embeddings_v2")
