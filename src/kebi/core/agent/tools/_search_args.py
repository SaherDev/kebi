"""Shared LLM-facing arg descriptions for the consult-family tools.

`find_saved` and `suggest_places` accept the same arg schema (intent +
category bias + hard-constraint tags + named-area overrides + limit).
Keeping the descriptions in one place means the agent sees byte-identical
argument docs for both tools — so it can pick between them on routing
semantics rather than on prompt drift.

These are plain constants, not a Pydantic args_schema, because
LangChain's @tool decorator does not compose cleanly with
`args_schema=PydanticModel` when the function also takes InjectedState /
InjectedToolCallId — the schema path strips the injected params from
the call site. Annotated[..., Field(...)] is the only shape that
survives both LLM tool-call schema generation AND runtime injection.
Verified empirically; do not refactor to args_schema= without
re-validating end-to-end against a live agent turn.
"""

from __future__ import annotations

QUERY_DESC = (
    "Free-text intent in noun-phrase form, e.g. 'cozy ramen for dinner'. "
    "Drives the pgvector + FTS retrieval (RRF-fused) on saved places, and "
    "the namer LLM prompt on suggested places. Keep it semantically "
    "rich but tight; do not embed constraint values that already live in `tags`."
)

CATEGORIES_DESC = (
    "OR-combined coarse categories the result must match one of (e.g. "
    "['restaurant', 'cafe']). Omit when the query alone is specific enough."
)

TAGS_DESC = (
    "AND-combined tag values the result must carry. Values are flat strings "
    "drawn from the controlled tag vocabulary in core/places/tags.py — e.g. "
    "'Thai', 'outdoor_seating', 'cozy', 'vegetarian', 'wheelchair_entrance'. "
    "Pass dietary / accessibility values here whenever the user has stated "
    "such a constraint in memory — these must always be applied."
)

NEIGHBORHOOD_DESC = (
    "Named neighborhood to scope to (e.g. 'Sukhumvit'). Pass when the user "
    "named a specific area smaller than a city."
)

CITY_DESC = (
    "Named city to scope to (e.g. 'Chiang Mai'). Pass when the user named a "
    "city different from the turn's working location."
)

COUNTRY_DESC = (
    "Named country to scope to (e.g. 'Thailand'). Rarely needed — most turns "
    "are city-scoped or finer."
)

LIMIT_DESC = (
    "How many candidates to return — pick based on context: one confident "
    "pick (1), a short comparison (3-5), a browse (10+). Capped by config; "
    "defaults if omitted."
)
