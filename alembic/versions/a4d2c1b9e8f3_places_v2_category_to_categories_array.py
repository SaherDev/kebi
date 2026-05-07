"""places_v2: category (text) → categories (text[])

Promotes places_v2.category from a single text column to a text[] array so
a place can carry every category Google's `types[]` returned for it (e.g.
a wine bar that's also a bar and a store). Query semantics on the
application side switch from equality to array overlap (&&).

Steps:
  1. Add `categories text[] NOT NULL DEFAULT '{}'`.
  2. Backfill from the existing single-valued `category`.
  3. Rebuild the `search_vector` generated tsvector column to reference
     `categories` via array_to_string instead of `category`.
  4. Drop the old `category` column.
  5. Add a GIN index on `categories` for `&&` overlap queries.

The `search_vector` column is GENERATED ALWAYS, so we drop it and the
dependent FTS index, then recreate both pointing at `categories`. Field
weights are unchanged (B, alongside `tags`).

Revision ID: a4d2c1b9e8f3
Revises: f3b8e1c4d2a9
Create Date: 2026-05-06 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a4d2c1b9e8f3"
down_revision: str | tuple[str, ...] | None = "f3b8e1c4d2a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. New column. NOT NULL with default '{}' so existing rows pass the
    #    constraint instantly without a rewrite-pass.
    op.execute(
        "ALTER TABLE places_v2 "
        "ADD COLUMN categories text[] NOT NULL DEFAULT '{}'"
    )

    # 2. Backfill from the single-valued column.
    op.execute(
        "UPDATE places_v2 "
        "SET categories = ARRAY[category] "
        "WHERE category IS NOT NULL AND categories = '{}'"
    )

    # 3. The existing search_vector is GENERATED from `category`, so
    #    Postgres won't let us drop `category` until the dependent
    #    expression is gone. Drop the FTS index and the generated column,
    #    recreate both against `categories`.
    #
    #    Postgres marks the built-in array_to_string as STABLE (legacy
    #    locale concerns for non-text arrays), but STORED generated
    #    columns require IMMUTABLE expressions. For `text[]` joined on
    #    a constant separator the function is provably immutable, so we
    #    wrap it in an IMMUTABLE SQL function and use that in the
    #    expression. Standard pattern; safe for our shape.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION places_v2_categories_to_text(text[])
        RETURNS text
        LANGUAGE SQL
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$ SELECT array_to_string($1, ' ') $$
        """
    )

    op.execute("DROP INDEX IF EXISTS places_v2_fts_idx")
    op.execute("ALTER TABLE places_v2 DROP COLUMN IF EXISTS search_vector")

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
                coalesce(places_v2_categories_to_text(categories), '')), 'B') ||
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

    # 4. Drop the now-orphan single-value column.
    op.execute("ALTER TABLE places_v2 DROP COLUMN category")

    # 5. GIN index on the array — needed for `categories && ARRAY[...]`
    #    overlap filters used by PlacesRepo.find and the hybrid search
    #    place-side filter.
    op.execute(
        "CREATE INDEX places_v2_categories_idx "
        "ON places_v2 USING gin(categories)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS places_v2_categories_idx")

    # Recreate the singular column and backfill from the first array
    # element. Multi-category rows lose the extras — this is an
    # information-losing downgrade, expected.
    op.execute("ALTER TABLE places_v2 ADD COLUMN category text")
    op.execute(
        "UPDATE places_v2 "
        "SET category = categories[1] "
        "WHERE array_length(categories, 1) >= 1"
    )

    # Rebuild search_vector against the singular `category` again.
    op.execute("DROP INDEX IF EXISTS places_v2_fts_idx")
    op.execute("ALTER TABLE places_v2 DROP COLUMN IF EXISTS search_vector")

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

    op.execute("ALTER TABLE places_v2 DROP COLUMN categories")
    op.execute(
        "DROP FUNCTION IF EXISTS places_v2_categories_to_text(text[])"
    )
