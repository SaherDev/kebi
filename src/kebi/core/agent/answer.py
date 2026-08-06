"""The turn's answer, assembled once from every tool result (ADR-144).

A place tool returns only what *it* found, so the user's saved coffee place in
Hoi An and the suggestion two doors down arrive in different payloads with no
way to render them together. Grouping can only happen after every tool has
run, which is what this module does — called from `finalize_node`, where the
tool payloads are already collected.

**Relations, not rendering.** The answer carries what only kebi can compute —
which area each item was found at, whether the turn is a journey, where each
item sits along it — and nothing about layout. A web client renders sections,
a phone renders a carousel, a map ignores the grouping entirely and draws
items by kind. None of them re-derives anything, and none of them is forced
into someone else's idea of a screen.

That is why the shape is **flat items plus an ordered group index** rather
than a tree of groups with items nested inside. A tree fixes one grouping
(by area — but a client may want by day, by category, or none), duplicates
items or splits authority the moment a second view exists, and bakes item
order into the contract. The index costs one `groupBy` on the client and
keeps every other view free.

Pure functions, no I/O.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.areas.models import AreaEntity, AreaSummary
from kebi.core.places.models import PlaceCore, UserPlace

logger = logging.getLogger(__name__)

# Order the user's own places lead with. Their saves are the strongest signal
# and the part no general assistant can show, so they head their group; the
# rest keeps the order the tools produced. A client that wants a different
# order has every field it needs to re-sort.
_SOURCE_RANK = {"saved": 0, "suggested": 1, "discovered": 2}


class AnswerGroup(BaseModel):
    """One section of the answer — a named area, or the road between two.

    Metadata and order only; the items live in the flat list beside it and
    point back here by `key`.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    kind: Literal["area", "on_the_way", "nearby"]
    position: int
    area: AreaSummary | None = None
    from_key: str | None = None
    to_key: str | None = None


class AnswerItem(BaseModel):
    """One thing in the answer, with where it belongs and where it sits.

    A thin envelope over the candidate the tools produced — `place` / `area` /
    `kind` are unchanged, so a client already reading `tool_results` sees the
    same objects. What is added is the relation: `group_key` and
    `route_progress`.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    group_key: str
    kind: Literal["venue", "area"]
    source: Literal["saved", "suggested", "discovered"]
    route_progress: float = 0.0
    place: PlaceCore | None = None
    area: AreaSummary | None = None
    extent: list[float] | None = None
    user_data: UserPlace | None = None
    reason: str | None = None


class Answer(BaseModel):
    """The whole turn's answer as relations a client can render any way."""

    model_config = ConfigDict(frozen=True)

    shape: Literal["journey", "areas", "list"]
    groups: list[AnswerGroup] = Field(default_factory=list)
    items: list[AnswerItem] = Field(default_factory=list)


# The group a candidate with no area anchor falls into — an ordinary near-me
# turn. Present so `groups` is never empty and clients need no special case
# for "this answer isn't grouped".
_NEARBY_KEY = "nearby"


def build_answer(
    tool_results: list[dict[str, Any]],
    anchors: list[AreaEntity],
    *,
    journey: bool,
) -> Answer | None:
    """Assemble the turn's answer, or None when there is nothing to render.

    Returns None rather than an empty `Answer` for a turn that surfaced no
    places at all (a research answer, a clarifying question) — an empty
    section list is a thing to render, and there is nothing here to render.
    """
    candidates = _candidates(tool_results)
    if not candidates:
        return None

    groups = _groups(anchors, journey=journey)
    known = {g.key for g in groups}

    # An area that IS a section is not also a row inside itself. The group
    # already carries its `AreaSummary`, so emitting it again would render
    # "Hoi An" as a card under the heading "Hoi An".
    rows = [c for c in candidates if not (c.area and c.area.entity_key in known)]

    items = [_item(c, _group_key(c, known)) for c in rows]
    if any(i.group_key == _NEARBY_KEY for i in items):
        # Appended only when something actually landed outside every named
        # area, so a clean area answer grows no stray section. Decided from
        # the rows that survive, not from the raw candidates — the areas that
        # became headings look unanchored and would conjure an empty one.
        groups.append(AnswerGroup(key=_NEARBY_KEY, kind="nearby", position=len(groups)))

    # A stretch with nothing on it is not a section — it is a heading over
    # empty space. An *area* with nothing in it stays, because the area is
    # itself the recommendation ("stay in Son Tra") and its group carries the
    # card; a road is only ever a container for what is on it.
    filled = {i.group_key for i in items}
    groups = [g for g in groups if g.kind != "on_the_way" or g.key in filled]
    groups = [g.model_copy(update={"position": n}) for n, g in enumerate(groups)]

    order = {g.key: g.position for g in groups}
    items.sort(
        key=lambda i: (
            order.get(i.group_key, len(order)),
            _SOURCE_RANK.get(i.source, 9),
            i.route_progress,
        )
    )

    shape: Literal["journey", "areas", "list"]
    if journey and len(anchors) > 1:
        shape = "journey"
    elif anchors:
        shape = "areas"
    else:
        shape = "list"
    return Answer(shape=shape, groups=groups, items=items)


def _candidates(tool_results: list[dict[str, Any]]) -> list[ConsultCandidate]:
    """Every candidate this turn produced, deduped, saves winning.

    Each tool dedupes internally but not against the others, so a place the
    user saved comes back from `find_saved` AND again from `suggest_places`.
    The saved copy wins because it carries `user_data` — "you've been meaning
    to go here" is a better card than "here's a suggestion" — which is the
    same rule `suggest_places._already_surfaced_ids` applies in-turn.
    """
    found: list[ConsultCandidate] = []
    for entry in tool_results:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        try:
            found.extend(ConsultResult.model_validate(payload).candidates)
        except Exception:  # noqa: BLE001 - a non-consult payload (research), skip
            continue

    by_identity: dict[str, ConsultCandidate] = {}
    ordered: list[str] = []
    for candidate in found:
        identity = _identity(candidate)
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = candidate
            ordered.append(identity)
            continue
        if existing.source != "saved" and candidate.source == "saved":
            by_identity[identity] = candidate
    return [by_identity[i] for i in ordered]


def _identity(candidate: ConsultCandidate) -> str:
    if candidate.area is not None:
        return f"area:{candidate.area.entity_key}"
    place = candidate.place
    if place is None:
        return f"item:{candidate.id}"
    return f"place:{place.provider_id or place.id or place.place_name}"


def _group_key(candidate: ConsultCandidate, known: set[str]) -> str:
    """Which section a row belongs to.

    An anchor pointing at a section the index no longer has (the anchor cap
    dropped it, say) falls back to `nearby` — better in the answer than
    missing from it.
    """
    key = candidate.anchor_area_key or _NEARBY_KEY
    return key if key in known else _NEARBY_KEY


def _groups(anchors: list[AreaEntity], *, journey: bool) -> list[AnswerGroup]:
    """The ordered sections: each named area, and on a journey the roads.

    Group order is the agent's naming order, which on a journey IS travel
    order — the agent lists the areas in the order people go through them.
    """
    groups: list[AnswerGroup] = []
    position = 0
    for index, anchor in enumerate(anchors):
        groups.append(
            AnswerGroup(
                key=anchor.entity_key,
                kind="area",
                position=position,
                area=AreaSummary.from_entity(anchor),
            )
        )
        position += 1
        if journey and index + 1 < len(anchors):
            nxt = anchors[index + 1]
            groups.append(
                AnswerGroup(
                    key=f"{anchor.entity_key}>{nxt.entity_key}",
                    kind="on_the_way",
                    position=position,
                    from_key=anchor.entity_key,
                    to_key=nxt.entity_key,
                )
            )
            position += 1
    return groups


def _item(candidate: ConsultCandidate, group_key: str) -> AnswerItem:
    return AnswerItem(
        id=candidate.id,
        group_key=group_key,
        kind=candidate.kind,
        source=candidate.source,
        route_progress=candidate.route_progress or 0.0,
        place=candidate.place,
        area=candidate.area,
        extent=candidate.extent,
        user_data=candidate.user_data,
        reason=candidate.reason,
    )
