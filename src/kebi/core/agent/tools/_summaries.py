"""Shared phrasing for tool reasoning-step summaries.

Each user-visible step is two lines: a bold `title` action carries the verb
("searched nearby"), and this `summary` result line sits under it. So summaries
are terse fragments that never repeat the verb — lowercase, no trailing period,
a short capped name preview (the full list lives in the place cards), no
questions or first-person monologue (the orchestrator's `message` frame carries
the conversational answer). `find_saved`, `suggest_places`, and `discover_places`
share this register so the thinking panel reads uniformly.
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
    "discover_places": "searched nearby",
    "suggest_places": "suggested a few spots",
    "suggest_areas": "picked out some areas",
    "research": "dug into the local intel",
}

# Shared empty-outcome result lines for the location-anchored tools.
NEED_LOCATION = "need a rough location first"
NONE_FIT = "none fit your requirements"
# Route-shaped turn whose legs are too long for venue stops (ADR-136). Reads as
# a scale observation, not an error — the agent's prose asks which stretch.
ROUTE_TOO_LONG = "that route is city-scale, not stop-scale"
# Route-shaped turn where nothing validated near the route itself.
NOTHING_ON_ROUTE = "nothing turned up along that route"


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


def areas_summary(names: Sequence[str], *, refused: int = 0) -> str:
    """Success result line for `suggest_areas` — same register, area noun.

    e.g. ``"3 areas — An Thuong, Son Tra, +1 more"``. `refused` (> 0) appends
    the count that could not be verified, because a silent drop is exactly
    what the roadmap forbids: the agent needs to know a name did not check out
    so it can say so rather than quietly answer about fewer places.
    """
    count = len(names)
    plural = "" if count == 1 else "s"
    preview = ", ".join(_short_name(n) for n in names[:_NAMES_PREVIEW])
    extra = "" if count <= _NAMES_PREVIEW else f", +{count - _NAMES_PREVIEW} more"
    tail = f" ({refused} didn't check out)" if refused else ""
    return f"{count} area{plural} — {preview}{extra}{tail}"
