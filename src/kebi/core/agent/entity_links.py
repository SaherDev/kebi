"""Entity links — the whole of what chat renders beyond plain text.

Chat is text plus tappable entity names. Nothing else crosses the wire for
display: no per-tool card payloads, no bespoke render shape per tool. A tool
that lands next month changes what the agent *says*, never what the client
*draws*.

The link format is `kebi://{kind}/{entity_key}` with exactly two kinds:

    venue  →  kebi://venue/<place id>            (the catalog id)
    area   →  kebi://area/<cc>/<city>/<hood>     (the knowledge geo key)

Both keys are the ones those surfaces already use — a venue key is what
`POST /v1/user/places` takes, an area key is `build_geo_key`'s output — so a
tap resolves against existing endpoints with no new identity scheme.

Links are attached **deterministically, after** the agent writes its answer:
the LLM writes plain prose naming places, and `linkify` wraps the names it
recognises from this turn's tool results. The model never transcribes an id,
so a link can never point at a place that wasn't retrieved.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel

from kebi.core.knowledge.schemas import build_geo_key
from kebi.core.places._place_utils import display_place_name
from kebi.core.places.models import normalize_icon

EntityKind = Literal["venue", "area"]

URI_SCHEME = "kebi"

# Tools whose payload carries place candidates. Kept local (rather than
# imported from the chat service) so the linker stays usable on any list of
# captured tool results.
#
# EVERY tool returning a `ConsultResult` must be listed here — a place tool
# missing from this set silently loses its links: its candidates never enter
# the index, so the agent names them in prose and they arrive as plain text.
_PLACE_TOOLS = frozenset(
    {"find_saved", "suggest_places", "discover_places", "find_known"}
)

# A venue key is opaque; an area key is a slash path. The knowledge layer
# namespaces place keys as `place:<id>` — anything else is a geo key.
_PLACE_KEY_PREFIX = "place:"

# Names shorter than this are never linked. Two-character venue names exist
# but linking them turns ordinary words ("Om", "Le") into taps.
_MIN_LINKABLE_NAME = 3

# People say "Luigi's", not "Luigi's Hot Pizza Canggu" — so the agent is told
# to write the spoken short form, and these prefixes are indexed to keep those
# mentions linkable. Capped at three words: beyond that the "short" form is
# the name.
_MAX_SHORT_FORM_WORDS = 3

# A prefix that is only a generic noun would turn every mention of the word
# into a tap on one arbitrary venue.
_GENERIC_PREFIXES = frozenset(
    {
        "the",
        "a",
        "an",
        "bank",
        "hotel",
        "cafe",
        "café",
        "bar",
        "restaurant",
        "club",
        "warung",
        "motel",
        "pt",
    }
)

# A ONE-WORD short form has to be distinctive enough to stand alone. "After
# Rock, Bali" yields "After", and without this every "after" in the answer
# becomes a tap on a bar the sentence was not talking about — the same
# wrong-destination failure as linking an area inside a street name, and worse
# than no link at all. Multi-word prefixes are unaffected: "After Rock" is
# unambiguous.
_COMMON_WORDS = frozenset(
    """after all also and any are before best but can come down first for from
    get going good great have here how into just like little local long look
    made make more most much near new next nice now off old once only open out
    over own part place right same see some still take than that then there
    they this time top try under until very want well what when where which
    while will with your""".split()
)


def _short_forms(name: str) -> list[str]:
    """Leading word-prefixes of a place name, shortest useful first.

    "Luigi's Hot Pizza Canggu" yields "Luigi's" and "Luigi's Hot"; the full
    name is already indexed by the caller. Prefixes ending on a generic noun
    are skipped ("The", "Bank"), and the caller drops any that more than one
    place would answer to.
    """
    words = name.split()
    forms: list[str] = []
    for count in range(1, min(len(words), _MAX_SHORT_FORM_WORDS + 1)):
        prefix = " ".join(words[:count])
        if len(prefix) < _MIN_LINKABLE_NAME:
            continue
        last = words[count - 1].strip("().,'s").lower()
        if last in _GENERIC_PREFIXES:
            continue
        if count == 1 and (last in _COMMON_WORDS or last in _GENERIC_PREFIXES):
            continue
        forms.append(prefix)
    return forms


class ChatEntity(BaseModel):
    """One linkable entity surfaced in a chat answer (explicit DTO, ADR-105).

    `uri` is the value the client hands to its link handler; `key` and `kind`
    are the same thing pre-split so the client does not have to parse. `name`
    is the canonical display name, which may differ from the text the answer
    actually used ("Luigi's" in prose, "Luigi's Hot Pizza" here).

    `icon` is the single emoji the client draws beside the name. A venue's
    comes off its catalog row, where an LLM already picked it (ADR-117); an
    area has no row, so its icon comes from the turn's location resolver,
    which is already looking at that area (ADR-146). Nullable on both kinds by
    design — a path with no model behind it leaves it unset and the client
    falls back to its own mapping, exactly as it already does for venues.
    """

    kind: EntityKind
    key: str
    name: str
    uri: str
    icon: str | None = None


def venue_uri(place_id: str) -> str:
    """`kebi://venue/<place id>`."""
    return f"{URI_SCHEME}://venue/{place_id}"


def area_uri(geo_key: str) -> str:
    """`kebi://area/<geo key>` — the key keeps its slashes as path segments."""
    return f"{URI_SCHEME}://area/{geo_key.strip('/')}"


def _venue(place_id: str, name: str, icon: str | None = None) -> ChatEntity:
    return ChatEntity(
        kind="venue",
        key=place_id,
        name=name,
        uri=venue_uri(place_id),
        icon=normalize_icon(icon),
    )


def _area(geo_key: str, name: str, icon: str | None = None) -> ChatEntity:
    key = geo_key.strip("/")
    return ChatEntity(
        kind="area", key=key, name=name, uri=area_uri(key), icon=normalize_icon(icon)
    )


def turn_recommendation_id(tool_results: list[dict[str, Any]]) -> str | None:
    """The turn's recommendation id, for attributing the turn's outcome.

    The first place-tool result's id is the turn's. It rides the chat
    response's `data` as an identifier of the recommendation itself —
    tracing and evals — not as save ceremony: the save path stopped taking
    it (ADR-151).
    """
    for result in tool_results:
        if result.get("tool") not in _PLACE_TOOLS:
            continue
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        rec_id = payload.get("recommendation_id")
        if isinstance(rec_id, str) and rec_id:
            return rec_id
    return None


def _candidate_entities(payload: dict[str, Any]) -> list[tuple[str, ChatEntity]]:
    """`(alias, entity)` pairs for every candidate in a place-tool payload.

    Each candidate contributes its canonical name plus any name aliases, so an
    answer that uses the TikTok caption's spelling still links.
    """
    pairs: list[tuple[str, ChatEntity]] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        place = candidate.get("place")
        if not isinstance(place, dict):
            continue
        place_id = place.get("id")
        name = place.get("place_name")
        if not isinstance(place_id, str) or not isinstance(name, str) or not name:
            continue
        # The entity carries the cleaned display name (it is what a client
        # renders), but BOTH spellings are indexed so the answer links whether
        # the agent wrote the tidy form or echoed the provider's.
        icon = place.get("icon")
        entity = _venue(
            place_id,
            display_place_name(name),
            icon if isinstance(icon, str) else None,
        )
        pairs.append((name, entity))
        if entity.name != name:
            pairs.append((entity.name, entity))
        for alias in place.get("place_name_aliases") or []:
            value = alias.get("value") if isinstance(alias, dict) else None
            if isinstance(value, str) and value:
                pairs.append((value, entity))
    return pairs


def _research_entity(payload: dict[str, Any]) -> list[tuple[str, ChatEntity]]:
    """`(alias, entity)` for the entity a research call resolved, if any.

    Research resolves to either a place (`place:<id>`) or a geo key, so it
    feeds both kinds off the one field.
    """
    key = payload.get("entity_key")
    name = payload.get("entity_name")
    if not isinstance(key, str) or not key or not isinstance(name, str) or not name:
        return []
    if key.startswith(_PLACE_KEY_PREFIX):
        return [(name, _venue(key[len(_PLACE_KEY_PREFIX) :], name))]
    return [(name, _area(key, name))]


def _working_location_entities(
    working_location: dict[str, Any] | None,
) -> list[tuple[str, ChatEntity]]:
    """`(alias, entity)` for the turn's city and neighborhood.

    The area the answer is *about* is almost never named by a tool — it comes
    from the resolver — so it is seeded here. Without a country code there is
    no canonical key, and an unkeyed area is not linkable.

    The resolver also picks each level's icon (ADR-146), so the entities it
    seeds arrive drawable; areas from anywhere else borrow those icons by key
    in `build_entity_index`.

    Defensive `isinstance`: the state slot can still hold the carry-forward
    sentinel string on a first turn (see `_carried_working_location`).
    """
    if not isinstance(working_location, dict) or not working_location:
        return []
    country_code = working_location.get("country_code")
    city = working_location.get("city")
    if not isinstance(country_code, str) or not country_code:
        return []
    if not isinstance(city, str) or not city:
        return []
    pairs: list[tuple[str, ChatEntity]] = []
    neighborhood = working_location.get("neighborhood")
    if isinstance(neighborhood, str) and neighborhood:
        pairs.append(
            (
                neighborhood,
                _area(
                    build_geo_key(country_code, city, neighborhood),
                    neighborhood,
                    _icon_of(working_location, "neighborhood_icon"),
                ),
            )
        )
    pairs.append(
        (
            city,
            _area(
                build_geo_key(country_code, city),
                city,
                _icon_of(working_location, "city_icon"),
            ),
        )
    )
    return pairs


def _icon_of(working_location: dict[str, Any], field: str) -> str | None:
    """One icon field off the working-location state slot, if it is a string."""
    value = working_location.get(field)
    return value if isinstance(value, str) else None


def build_entity_index(
    tool_results: list[dict[str, Any]],
    working_location: dict[str, Any] | None = None,
) -> list[tuple[str, ChatEntity]]:
    """Every `(alias, entity)` pair this turn can link, longest alias first.

    Longest-first is what makes overlapping names resolve correctly: "Luigi's
    Hot Pizza" must win over a "Luigi's" contributed by another candidate.
    Duplicate aliases keep their first (highest-priority) entity — place-tool
    results are indexed before research and the working location, so a real
    retrieved venue beats a same-named area.
    """
    pairs: list[tuple[str, ChatEntity]] = []
    for result in tool_results:
        payload = result.get("payload")
        if not isinstance(payload, dict):
            continue
        tool = result.get("tool")
        if tool in _PLACE_TOOLS:
            pairs.extend(_candidate_entities(payload))
        elif tool == "research":
            pairs.extend(_research_entity(payload))
    pairs.extend(_working_location_entities(working_location))

    # An area the research tool resolved is usually the very area the turn is
    # working in, keyed identically — so it borrows the icon the resolver
    # already picked instead of arriving as the one undrawable entity in the
    # answer.
    area_icons = {
        entity.key: entity.icon
        for _, entity in pairs
        if entity.kind == "area" and entity.icon
    }
    if area_icons:
        pairs = [
            (alias, entity)
            if entity.icon or entity.kind != "area" or entity.key not in area_icons
            else (alias, entity.model_copy(update={"icon": area_icons[entity.key]}))
            for alias, entity in pairs
        ]

    # Spoken short forms, added only where exactly one place answers to them.
    # An ambiguous prefix ("Bank Mandiri…" vs "Bank BNI…") would send a tap to
    # whichever place happened to be indexed first, so it is dropped instead.
    claimants: dict[str, set[str]] = {}
    for alias, entity in pairs:
        if entity.kind != "venue":
            continue
        for short in _short_forms(alias):
            claimants.setdefault(short.casefold(), set()).add(entity.uri)
    short_pairs: list[tuple[str, ChatEntity]] = []
    for alias, entity in pairs:
        if entity.kind != "venue":
            continue
        for short in _short_forms(alias):
            if len(claimants.get(short.casefold(), ())) == 1:
                short_pairs.append((short, entity))
    pairs.extend(short_pairs)

    seen: set[str] = set()
    deduped: list[tuple[str, ChatEntity]] = []
    for alias, entity in pairs:
        cleaned = alias.strip()
        if len(cleaned) < _MIN_LINKABLE_NAME:
            continue
        folded = cleaned.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        deduped.append((cleaned, entity))
    deduped.sort(key=lambda pair: len(pair[0]), reverse=True)
    return deduped


# A name is only a link when it stands as a word: `Luigi's` inside
# `Luigi'sland` is not a mention. Word characters on either side block the
# match; punctuation and whitespace do not.
def _alias_pattern(alias: str) -> str:
    return rf"(?<!\w){re.escape(alias)}(?!\w)"


# Tokens after which a following capitalised word is part of an address, not a
# mention of the area. "Jl. Raya Canggu" is a street; the user tapping it
# expects the road, and sending them to the neighbourhood sheet is a link that
# resolves to the wrong thing — worse than no link.
_ADDRESS_MARKERS = frozenset(
    {
        "jl",
        "jln",
        "jalan",
        "gg",
        "gang",
        "st",
        "street",
        "rd",
        "road",
        "ave",
        "avenue",
        "raya",
        "soi",
        "blvd",
        "boulevard",
        "lane",
        "ln",
    }
)

_TRAILING_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z.']*)\s*$")


def _area_match_is_incidental(before: str) -> bool:
    """True when an area name here is part of a longer name, not a mention.

    Areas are the collision-prone kind: their names turn up inside street
    names ("Jl. Raya Canggu"), venue names ("BNI CANGGU"), and business
    suffixes ("Motel Mexicola | Canggu"). Venues are specific enough not to
    need this, so the guard is deliberately area-only.

    The signal is the preceding token: an address marker, or a capitalised
    word that is not starting a sentence, both mean the area name is being
    used as part of a proper noun. Suppressing degrades to plain text, which
    is the safe direction — a link that opens the wrong screen costs more
    trust than a missing one.
    """
    stripped = before.rstrip()
    # Provider names join their area with a separator ("Motel Mexicola |
    # Canggu"); the tail is part of the venue's name, not a mention.
    if stripped and stripped[-1] in "|/–—-":
        return True
    match = _TRAILING_TOKEN_RE.search(before)
    if match is None:
        return False
    token = match.group(1)
    if token.rstrip(".").lower() in _ADDRESS_MARKERS:
        return True
    if not token[0].isupper():
        return False
    # An ALL-CAPS token is a name fragment ("BNI CANGGU"), never a sentence
    # opener, so it counts even at the start of a line.
    if token.isupper() and len(token) > 1:
        return True
    # A capitalised word right after sentence-ending punctuation is just the
    # start of a sentence, not evidence of a compound name.
    preceding = before[: match.start(1)].rstrip()
    return not (preceding == "" or preceding[-1] in ".!?:;\n")


# Mechanical voice rules the prompt states and the model keeps breaking. They
# carry no meaning — an em dash is a comma, `**bold**` is emphasis markup, an
# arrow is the word "then" — so enforcing them in code costs nothing and,
# unlike an instruction, cannot lose to the rest of the prompt. Judgment stays
# in the prompt; only the typography is normalised here.
_VOICE_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # An em/en dash between words becomes a comma; one that already sits
    # against punctuation just goes.
    (re.compile(r"\s*[—–]\s*(?=[,.;:!?])"), ""),
    (re.compile(r"\s*[—–]\s*"), ", "),
    (re.compile(r"\*\*"), ""),
    (re.compile(r"\s*→\s*"), " then "),
)


def normalize_voice(text: str) -> str:
    """Strip the typography the voice rules forbid, leaving meaning untouched.

    Runs before linkification so link matching still sees the names exactly as
    the model wrote them.
    """
    for pattern, replacement in _VOICE_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    # A substitution can leave a doubled comma ("x, , y") where the model had
    # already punctuated around the dash.
    text = re.sub(r",\s*,", ",", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def linkify(
    text: str, index: list[tuple[str, ChatEntity]]
) -> tuple[str, list[ChatEntity]]:
    """Wrap recognised entity names in markdown links to their `kebi://` URIs.

    Returns the rewritten text and the entities actually linked, in the order
    they appear. Only the first mention of an entity is linked — repeat
    mentions read as prose, not as a wall of taps — and matches never overlap,
    so an entity name inside an already-linked longer name is left alone.

    A no-op when the answer names nothing retrieved: the text comes back
    byte-identical and the entity list is empty.
    """
    if not text or not index:
        return text, []

    pattern = re.compile(
        "|".join(f"({_alias_pattern(alias)})" for alias, _ in index),
        re.IGNORECASE,
    )
    linked: dict[str, ChatEntity] = {}
    ordered: list[ChatEntity] = []

    def _replace(match: re.Match[str]) -> str:
        group = match.lastindex
        if group is None:
            return match.group(0)
        entity = index[group - 1][1]
        if entity.uri in linked:
            return match.group(0)
        if entity.kind == "area" and _area_match_is_incidental(
            match.string[: match.start()]
        ):
            return match.group(0)
        linked[entity.uri] = entity
        ordered.append(entity)
        return f"[{match.group(0)}]({entity.uri})"

    return pattern.sub(_replace, text), ordered
