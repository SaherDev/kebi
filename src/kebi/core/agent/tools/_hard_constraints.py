"""Hard-constraint tag filter shared by consult-family agent tools.

The agent's prompt receives the user's stored memory facts via the
`{memory_summary}` slot. The LLM is expected to translate any hard
constraints in those facts (e.g. "I'm vegetarian" → `vegetarian`) into
the `tags` arg on the tool call.

Constraint semantics are split by value class (ADR-118, partially
superseding ADR-101's uniform enforcement):

- SAFETY values (dietary + accessibility) stay hard: a place passes
  only when the tag is affirmatively present. Recommending a
  non-vegetarian place to a vegetarian, or an unverified venue to a
  wheelchair user, is harm — absence means exclusion.
- Everything else (service, feature, price, atmosphere, time, season,
  free-text) is a preference signal: it biases retrieval (DB tag
  predicate on persisted rows, provider keyword text) and the
  orchestrator's ordering/wording, but never excludes a candidate
  post-fetch. A freshly discovered place has no experiential tags yet —
  absence there is ignorance, not "no"; the LLM knowledge layer
  densifies tags as content flows through extraction.

`suggest_places` and `discover_places` both share these helpers so the
filter behaviour stays uniform — the agent picks between the tools on
routing semantics, not on whether constraints are honoured.
"""

from __future__ import annotations

from kebi.core.places.models import PlaceCore
from kebi.core.places.tags import AccessibilityTag, DietaryTag

# Tag values enforced as hard constraints. Dietary + accessibility are
# safety semantics; every other tag value is a soft preference.
SAFETY_TAG_VALUES: frozenset[str] = frozenset(
    v.value for v in (*DietaryTag, *AccessibilityTag)
)


def split_constraints(required: list[str]) -> tuple[list[str], list[str]]:
    """Partition required tag values into (hard, soft), order-preserving.

    Case-insensitive membership against SAFETY_TAG_VALUES; unknown /
    free-text values are soft (an unrecognized value must never become
    an accidental excluder).
    """
    hard: list[str] = []
    soft: list[str] = []
    for req in required:
        if req.strip().lower() in SAFETY_TAG_VALUES:
            hard.append(req)
        else:
            soft.append(req)
    return hard, soft


def hard_constraints_satisfied(place: PlaceCore, required: list[str]) -> bool:
    """True iff every required tag value is present in `place.tags`.

    AND across required tags. Comparison is case-insensitive on the tag
    `.value`, with enum members normalized to their `.value` string.
    Returns True when `required` is empty (no constraint → no filter).
    Callers pass the `hard` half of `split_constraints` here.
    """
    if not required:
        return True
    present: set[str] = set()
    for tag in place.tags:
        raw = tag.value
        value = raw.value if hasattr(raw, "value") else str(raw)
        present.add(value.strip().lower())
    return all(req.strip().lower() in present for req in required)
