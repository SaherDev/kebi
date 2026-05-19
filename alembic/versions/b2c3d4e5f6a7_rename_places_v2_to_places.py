"""rename the places_v2 layer to its canonical unqualified name (ADR-079)

ADR-078 removed the last of the v1 places store, so the `_v2` qualifier
disambiguates from nothing. This renames the catalog tables and every
dependent object to the plain name. Data and row identity are preserved
(unlike the ADR-078 drops) — this is a pure rename.

PostgreSQL does NOT auto-rename dependent indexes/constraints on
`ALTER TABLE ... RENAME TO`, so each is renamed explicitly. Object names
were verified against a live migrated database. The `search_vector`
generated column embeds no table name and references the helper function
by OID, so it survives the table and function rename untouched.

FK `user_places_place_id_fkey` carries no `_v2` and follows the renamed
parent by OID — left as-is.

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-05-19 00:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("places_v2", "places")
    op.rename_table("place_embeddings_v2", "place_embeddings")

    # Plain indexes (RENAME CONSTRAINT below covers pkey/unique-constraint
    # backing indexes, so they are not listed here).
    op.execute("ALTER INDEX places_v2_categories_idx RENAME TO places_categories_idx")
    op.execute("ALTER INDEX places_v2_fts_idx RENAME TO places_fts_idx")
    op.execute("ALTER INDEX places_v2_geo_idx RENAME TO places_geo_idx")
    op.execute("ALTER INDEX places_v2_tags_idx RENAME TO places_tags_idx")
    op.execute(
        "ALTER INDEX uq_places_v2_provider_id RENAME TO uq_places_provider_id"
    )
    op.execute(
        "ALTER INDEX place_embeddings_v2_vector_hnsw_idx "
        "RENAME TO place_embeddings_vector_hnsw_idx"
    )

    # Constraints (renaming a PK/UNIQUE constraint renames its index too).
    op.execute(
        "ALTER TABLE places RENAME CONSTRAINT places_v2_pkey TO places_pkey"
    )
    op.execute(
        "ALTER TABLE place_embeddings "
        "RENAME CONSTRAINT place_embeddings_v2_pkey TO place_embeddings_pkey"
    )
    op.execute(
        "ALTER TABLE place_embeddings RENAME CONSTRAINT "
        "place_embeddings_v2_place_id_key TO place_embeddings_place_id_key"
    )
    op.execute(
        "ALTER TABLE place_embeddings RENAME CONSTRAINT "
        "place_embeddings_v2_place_id_fkey TO place_embeddings_place_id_fkey"
    )

    op.execute(
        "ALTER FUNCTION places_v2_categories_to_text(text[]) "
        "RENAME TO places_categories_to_text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER FUNCTION places_categories_to_text(text[]) "
        "RENAME TO places_v2_categories_to_text"
    )

    op.execute(
        "ALTER TABLE place_embeddings RENAME CONSTRAINT "
        "place_embeddings_place_id_fkey TO place_embeddings_v2_place_id_fkey"
    )
    op.execute(
        "ALTER TABLE place_embeddings RENAME CONSTRAINT "
        "place_embeddings_place_id_key TO place_embeddings_v2_place_id_key"
    )
    op.execute(
        "ALTER TABLE place_embeddings "
        "RENAME CONSTRAINT place_embeddings_pkey TO place_embeddings_v2_pkey"
    )
    op.execute(
        "ALTER TABLE places RENAME CONSTRAINT places_pkey TO places_v2_pkey"
    )

    op.execute(
        "ALTER INDEX place_embeddings_vector_hnsw_idx "
        "RENAME TO place_embeddings_v2_vector_hnsw_idx"
    )
    op.execute(
        "ALTER INDEX uq_places_provider_id RENAME TO uq_places_v2_provider_id"
    )
    op.execute("ALTER INDEX places_tags_idx RENAME TO places_v2_tags_idx")
    op.execute("ALTER INDEX places_geo_idx RENAME TO places_v2_geo_idx")
    op.execute("ALTER INDEX places_fts_idx RENAME TO places_v2_fts_idx")
    op.execute(
        "ALTER INDEX places_categories_idx RENAME TO places_v2_categories_idx"
    )

    op.rename_table("place_embeddings", "place_embeddings_v2")
    op.rename_table("places", "places_v2")
