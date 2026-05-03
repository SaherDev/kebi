"""widen places_v2.search_vector to include tag types + formatted address

Two FTS recall fixes (smoke verified — see scripts/places_v2_calls.json):

  * **tag types** (weight B): the previous expression extracted only
    `tags[*].value`. The simple_unaccent tokenizer splits underscored
    values like `wheelchair_entrance` into `wheelchair` + `entrance` and
    the type word ("accessibility") never enters the index. Indexing
    `tags[*].type` alongside the value lets natural-language queries
    use category words ("accessible museum", "dietary options").

  * **formattedAddress** (weight C): Google's `addressComponents` puts
    `locality=Shibuya` and `administrative_area_level_1=Tokyo` for
    Tokyo places. The mapper only extracts `locality` → `city`, so
    "Tokyo" never reaches the search_vector. `formattedAddress` is
    already mapped to `LocationContext.address` and contains the full
    string ("…Shibuya, Tokyo 150-0041, Japan"), so indexing it covers
    metropolis names without a schema change.

Generated columns can't be altered in place — drop and re-add. Postgres
re-computes the column for every existing row at ALTER time.

Revision ID: f3b8e1c4d2a9
Revises: e9f0a1b2c3d4
Create Date: 2026-05-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f3b8e1c4d2a9"
down_revision: str | tuple[str, ...] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("places_v2", "search_vector")
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
                    jsonb_path_query_array(tags, '$[*].type')::text,
                    ''
                )), 'B') ||
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
                coalesce(location->>'country', '')), 'C') ||
            setweight(to_tsvector('simple_unaccent',
                coalesce(location->>'address', '')), 'C')
        ) STORED
        """
    )


def downgrade() -> None:
    op.drop_column("places_v2", "search_vector")
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
