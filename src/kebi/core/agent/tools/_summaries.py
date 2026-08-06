"""Shared phrasing for tool reasoning-step summaries.

Each user-visible step is two lines: a bold `title` action carries the verb
("searched nearby"), and this `summary` result line sits under it. So summaries
are terse fragments that never repeat the verb — lowercase, no trailing period,
a short capped name preview (the full list lives in the place cards), no
questions or first-person monologue (the orchestrator's `message` frame carries
the conversational answer). Every place tool shares this register so the
thinking panel reads uniformly.
"""

from collections.abc import Sequence

# Names beyond this collapse into "+N more" — the place cards carry the full
# list, so the result line stays a glanceable fragment.
_NAMES_PREVIEW = 2

# Cap a single previewed name; the full Google name (descriptors, translations)
# lives in `tool_results`, so the trace only needs the leading short name.
_NAME_MAX = 28


def _short_name(name: str) -> str:
    """Trim a place name to its short leading form for the preview.

    Google names trail descriptors and translations ("… (Halal Vegan) Shibuya
    Restaurant 涩谷 和牛"); drop a parenthetical tail, then cap the length at a
    word boundary. The full name is always available in `tool_results`.
    """
    for paren in ("(", "（"):
        idx = name.find(paren)
        if idx > 0:
            name = name[:idx]
    name = name.strip(" -–—,")
    if len(name) <= _NAME_MAX:
        return name
    clipped = name[:_NAME_MAX].rsplit(" ", 1)[0].rstrip() or name[:_NAME_MAX]
    return f"{clipped}…"


# Tool → bold action line (ADR-103). The summary never repeats this verb.
TITLES = {
    "find_saved": "searched your saved spots",
    "suggest_places": "suggested a few spots",
    "research": "dug into the local intel",
    "find_known": "checked what I know around here",
}

# Shared empty-outcome result lines for the location-anchored tools.
NEED_LOCATION = "need a rough location first"
NONE_FIT = "none fit your requirements"


def found_summary(names: Sequence[str], *, dropped: int = 0) -> str:
    """Success result line: count, a capped name preview, optional drop tail.

    e.g. ``found_summary(["Per Se", "Blue Hill", ...])`` (5 names) →
    ``"5 spots — Per Se, Blue Hill, +3 more"``. `dropped` (> 0) appends a
    terse "(N didn't fit)" tail for hard-constraint filtering.
    """
    count = len(names)
    plural = "" if count == 1 else "s"
    preview = ", ".join(_short_name(n) for n in names[:_NAMES_PREVIEW])
    extra = "" if count <= _NAMES_PREVIEW else f", +{count - _NAMES_PREVIEW} more"
    tail = f" ({dropped} didn't fit)" if dropped else ""
    return f"{count} spot{plural} — {preview}{extra}{tail}"
