"""Field-name contract enforced across the places_v2 retrieval surface.

`SEARCHABLE_FIELDS` is the canonical list of PlaceCore (and
LocationContext) attributes that participate in retrieval. Adding a
field means updating four places:

  1. PlaceCore (or LocationContext) in `models.py` — new attribute.
  2. The Alembic migration's `search_vector` generated column —
     include the field so FTS sees it.
  3. `EmbeddingService._build_text` — include the field so the vector
     embedding sees it (and add to its `_EMBED_FIELDS` set).
  4. `HybridSearchRepo` — add to `_FILTER_FIELDS` if discretely
     filterable, otherwise to `_FTS_ONLY_FIELDS`.

Every consuming module declares its handled set as a private constant
and asserts it lines up with `SEARCHABLE_FIELDS` at import time. Adding
a field here without wiring it up everywhere → AssertionError on app
startup with a message naming exactly which field is missing where.

`HybridSearchFilters` (discrete WHERE filters) is intentionally a
subset of the searchable set — `place_name` and `place_name_aliases`
are searched via the user-typed `query` parameter through the FTS
leg, so they're in `_FTS_ONLY_FIELDS` rather than `_FILTER_FIELDS`.
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
