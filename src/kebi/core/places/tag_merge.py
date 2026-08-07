"""Shared LLM-tag conversion + merge helpers.

Every path that lets an LLM assert experiential tags funnels through
`llm_tags_to_place_tags`: the extraction resolver (post-level tags), the
extraction classifier (per-place tags), and the place profiler (identity-only
enrichment, ADR-152). Keeping the conversion in one place keeps the
accessibility backstop in one place — a caller that skipped it could ship a
hallucinated "wheelchair accessible", which is real-world harm, not a
mis-rank.

Lives in `core/places` (not extraction) because it depends only on the
places tag vocabulary, and the profiler — a places-layer service — must not
import the extraction pipeline to spell a tag.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol

from .models import PlaceTag
from .tags import AccessibilityTag, TagType

logger = logging.getLogger(__name__)


class LLMTagLike(Protocol):
    type: str
    value: str


# Accessibility tag values, matched case-insensitively against ANY emitted
# value regardless of the type label the LLM chose — defense against
# mislabeling (e.g. type="feature", value="wheelchair_entrance").
_ACCESSIBILITY_VALUES: frozenset[str] = frozenset(a.value for a in AccessibilityTag)


def llm_tags_to_place_tags(tags: Iterable[LLMTagLike]) -> list[PlaceTag]:
    """Convert flat LLM-emitted tags → `PlaceTag` with `source="llm"`.

    Type values outside `TagType` fall through as plain strings —
    `PlaceTag.type` accepts `TagType | str`. Empty type/value pairs are
    skipped.

    Accessibility tags are categorically dropped (ADR-118): a
    hallucinated "wheelchair accessible" is real-world harm, not a
    mis-rank, so inferred sources may never assert accessibility. The
    prompts forbid it too; this is the code backstop for every caller.
    """
    out: list[PlaceTag] = []
    for t in tags:
        if not t.value or not t.type:
            continue
        try:
            tag_type: TagType | str = TagType(t.type)
        except ValueError:
            tag_type = t.type
        if (
            tag_type == TagType.accessibility
            or t.value.strip().lower() in _ACCESSIBILITY_VALUES
        ):
            logger.warning("llm_accessibility_tag_dropped %s=%s", t.type, t.value)
            continue
        out.append(PlaceTag(type=tag_type, value=t.value, source="llm"))
    return out


def merge_tags(per_place: list[PlaceTag], shared: list[PlaceTag]) -> list[PlaceTag]:
    """Union two tag lists, first list winning on conflict (ADR-080).

    Dedupe by `(type, value)`; a tag from `per_place` wins because its
    evidence is more specific (venue-level over post-level in extraction;
    already-attested over newly-inferred in profiling). Order: `per_place`
    first, then any `shared` tag whose `(type, value)` is not already
    present.
    """

    def _key(tag: PlaceTag) -> tuple[str, str]:
        type_str = tag.type.value if isinstance(tag.type, TagType) else tag.type
        return (type_str, str(tag.value))

    seen = {_key(t) for t in per_place}
    out = list(per_place)
    for t in shared:
        if _key(t) not in seen:
            seen.add(_key(t))
            out.append(t)
    return out
