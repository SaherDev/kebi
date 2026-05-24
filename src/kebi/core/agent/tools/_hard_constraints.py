"""Hard-constraint tag filter shared by consult-family agent tools.

The agent's prompt receives the user's stored memory facts via the
`{memory_summary}` slot. The LLM is expected to translate any hard
constraints in those facts (e.g. "I'm vegetarian" → `vegetarian`) into
the `tags` arg on the tool call. This module enforces those hard
constraints post-fetch: a place passes only when every required tag
value is present on the place.

`suggest_places` and `discover_places` both share this helper so the
filter behaviour stays uniform — the agent picks between the tools on
routing semantics, not on whether constraints are honoured.
"""

from __future__ import annotations

from kebi.core.places.models import PlaceCore


def hard_constraints_satisfied(place: PlaceCore, required: list[str]) -> bool:
    """True iff every required tag value is present in `place.tags`.

    AND across required tags. Comparison is case-insensitive on the tag
    `.value`, with enum members normalized to their `.value` string.
    Returns True when `required` is empty (no constraint → no filter).
    """
    if not required:
        return True
    present: set[str] = set()
    for tag in place.tags:
        raw = tag.value
        value = raw.value if hasattr(raw, "value") else str(raw)
        present.add(value.strip().lower())
    return all(req.strip().lower() in present for req in required)
