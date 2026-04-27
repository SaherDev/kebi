"""Field-name set injected into the places_v2 retrieval consumers.

`SEARCHABLE_FIELDS` is the canonical set of PlaceCore fields that
participate in retrieval. `EmbeddingService` and `HybridSearchRepo`
both take it via constructor (defaulting here) and gate each
field-specific block on membership. Adding or removing a field is one
edit here; both consumers pick it up. Tests can pass a smaller subset
to verify isolation.

Heterogeneous filters that aren't per-field — geo box, saved_at
range, user-side bools (visited / liked / approved) — stay
unconditional in the repo. They're feature filters, not field
filters, so they don't belong in this set.

The Alembic migration's `search_vector` generated column is the third
consumer; it's hardcoded SQL and can't import this constant. A test
substring-checks every name appears in the migration source so drift
fails CI.
"""

from __future__ import annotations

SEARCHABLE_FIELDS: frozenset[str] = frozenset(
    {
        "place_name",
        "place_name_aliases",
        "category",
        "tags",
        "neighborhood",
        "city",
        "country",
    }
)
