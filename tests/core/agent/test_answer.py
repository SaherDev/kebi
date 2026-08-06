"""The turn's assembled answer — relations, not rendering (ADR-144).

What matters here is that a client gets everything it needs to render the
answer any way it likes, and gets it without merging tool payloads itself:
one item per place however many tools found it, the group it belongs to, and
where it sits along a drive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kebi.core.agent.answer import build_answer
from kebi.core.areas.models import AreaEntity, AreaSummary
from kebi.core.places.models import PlaceCategory, PlaceCore, UserPlace


def _area(key: str, name: str) -> AreaEntity:
    return AreaEntity(
        entity_key=key,
        entity_type="city",
        name=name,
        country_code="vn",
        lat=15.88,
        lng=108.33,
    )


def _place(name: str, place_id: str) -> PlaceCore:
    return PlaceCore(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
    )


def _candidate(
    name: str,
    place_id: str,
    *,
    source: str,
    group: str | None = None,
    progress: float | None = None,
    saved: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "venue",
        "place": _place(name, place_id).model_dump(mode="json"),
        "source": source,
        "rrf_score": 0.0,
        "anchor_area_key": group,
        "route_progress": progress,
        "user_data": (
            UserPlace(
                user_place_id="up-1",
                user_id="u1",
                place_id=place_id,
                source="manual",
                saved_at=datetime.now(UTC),
            ).model_dump(mode="json")
            if saved
            else None
        ),
    }


def _result(tool: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tool": tool,
        "tool_call_id": f"tc-{tool}",
        "payload": {"candidates": candidates, "recommendation_id": "r1"},
    }


# ---- shape -----------------------------------------------------------------


def test_a_turn_with_no_places_has_no_answer_to_render() -> None:
    """A research answer or a clarifying question is prose, not a section
    list — an empty group list is a thing to render, and there is nothing."""
    assert build_answer([], [], journey=False) is None
    assert build_answer([_result("research", [])], [], journey=False) is None


def test_an_ordinary_turn_still_yields_one_group() -> None:
    """So clients need no special case for "this answer isn't grouped"."""
    answer = build_answer(
        [_result("suggest_places", [_candidate("A", "p1", source="suggested")])],
        [],
        journey=False,
    )
    assert answer is not None
    assert answer.shape == "list"
    assert [g.key for g in answer.groups] == ["nearby"]
    assert [i.group_key for i in answer.items] == ["nearby"]


def test_named_areas_without_travel_are_an_areas_answer() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [_candidate("A", "p1", source="suggested", group="vn/hoi-an")],
            )
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=False,
    )
    assert answer is not None
    assert answer.shape == "areas"
    assert [g.key for g in answer.groups] == ["vn/hoi-an", "vn/hue"]
    assert all(g.kind == "area" for g in answer.groups)


def test_a_journey_puts_the_road_between_the_areas() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _candidate("A", "p1", source="suggested", group="vn/hoi-an"),
                    _candidate(
                        "On The Road",
                        "p2",
                        source="suggested",
                        group="vn/hoi-an>vn/hue",
                    ),
                ],
            )
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    assert answer.shape == "journey"
    assert [(g.key, g.kind) for g in answer.groups] == [
        ("vn/hoi-an", "area"),
        ("vn/hoi-an>vn/hue", "on_the_way"),
        ("vn/hue", "area"),
    ]
    stretch = answer.groups[1]
    assert (stretch.from_key, stretch.to_key) == ("vn/hoi-an", "vn/hue")


def test_a_clean_area_answer_grows_no_stray_nearby_section() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [_candidate("A", "p1", source="suggested", group="vn/hoi-an")],
            )
        ],
        [_area("vn/hoi-an", "Hoi An")],
        journey=False,
    )
    assert answer is not None
    assert [g.key for g in answer.groups] == ["vn/hoi-an"]


def test_an_unanchored_item_gets_a_nearby_section_to_land_in() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _candidate("A", "p1", source="suggested", group="vn/hoi-an"),
                    _candidate("B", "p2", source="discovered"),
                ],
            )
        ],
        [_area("vn/hoi-an", "Hoi An")],
        journey=False,
    )
    assert answer is not None
    assert [g.key for g in answer.groups] == ["vn/hoi-an", "nearby"]
    assert len(answer.items) == 2


# ---- one item per place ----------------------------------------------------


def test_the_same_place_from_two_tools_appears_once_as_the_save() -> None:
    """Each tool dedupes internally but not against the others. "You've been
    meaning to go here" is a better card than "here's a suggestion"."""
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [_candidate("Banh Mi", "p1", source="suggested", group="vn/hoi-an")],
            ),
            _result(
                "find_saved",
                [
                    _candidate(
                        "Banh Mi", "p1", source="saved", group="vn/hoi-an", saved=True
                    )
                ],
            ),
        ],
        [_area("vn/hoi-an", "Hoi An")],
        journey=False,
    )
    assert answer is not None
    assert len(answer.items) == 1
    assert answer.items[0].source == "saved"
    assert answer.items[0].user_data is not None


def test_a_research_payload_is_skipped_not_fatal() -> None:
    answer = build_answer(
        [
            {"tool": "research", "tool_call_id": "tc", "payload": {"notes": [{}]}},
            _result("suggest_places", [_candidate("A", "p1", source="suggested")]),
        ],
        [],
        journey=False,
    )
    assert answer is not None
    assert len(answer.items) == 1


# ---- order -----------------------------------------------------------------


def test_saves_lead_their_group_and_groups_follow_travel_order() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _candidate(
                        "Hue Suggested", "p3", source="suggested", group="vn/hue"
                    ),
                    _candidate(
                        "Hoi An Suggested",
                        "p2",
                        source="suggested",
                        group="vn/hoi-an",
                    ),
                    _candidate(
                        "On The Road",
                        "p4",
                        source="suggested",
                        group="vn/hoi-an>vn/hue",
                    ),
                ],
            ),
            _result(
                "find_saved",
                [
                    _candidate(
                        "Hoi An Saved",
                        "p1",
                        source="saved",
                        group="vn/hoi-an",
                        saved=True,
                    )
                ],
            ),
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    assert [i.place.place_name if i.place else "" for i in answer.items] == [
        "Hoi An Saved",
        "Hoi An Suggested",
        "On The Road",
        "Hue Suggested",
    ]


def test_items_carry_the_relations_a_client_needs_to_re_sort() -> None:
    """The whole point of flat-items-plus-index: nothing is re-derived."""
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _candidate(
                        "A",
                        "p1",
                        source="suggested",
                        group="vn/hoi-an>vn/hue",
                        progress=0.5,
                    )
                ],
            )
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    item = answer.items[0]
    assert item.group_key == "vn/hoi-an>vn/hue"
    assert item.route_progress == 0.5
    assert item.source == "suggested"
    assert item.kind == "venue"


def test_an_area_group_carries_its_summary_for_the_heading() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [_candidate("A", "p1", source="suggested", group="vn/hoi-an")],
            )
        ],
        [_area("vn/hoi-an", "Hoi An")],
        journey=False,
    )
    assert answer is not None
    area = answer.groups[0].area
    assert isinstance(area, AreaSummary)
    assert area.name == "Hoi An"


# ---- areas as sections vs areas as items -----------------------------------


def _area_candidate(
    key: str, name: str, *, group: str | None = None, progress: float | None = None
) -> dict[str, Any]:
    return {
        "kind": "area",
        "area": AreaSummary.from_entity(_area(key, name)).model_dump(mode="json"),
        "source": "suggested",
        "rrf_score": 0.0,
        "anchor_area_key": group,
        "route_progress": progress,
    }


def test_an_area_that_is_a_section_is_not_also_a_row_inside_itself() -> None:
    """`suggest_areas` returns Hoi An as a card AND it is the group heading —
    emitting both renders "Hoi An" as an item under the heading "Hoi An"."""
    answer = build_answer(
        [_result("suggest_areas", [_area_candidate("vn/hoi-an", "Hoi An")])],
        [_area("vn/hoi-an", "Hoi An")],
        journey=False,
    )
    assert answer is not None
    assert [g.key for g in answer.groups] == ["vn/hoi-an"]
    assert answer.items == []
    assert answer.groups[0].area is not None


def test_an_area_found_on_the_road_is_an_item_in_that_stretch() -> None:
    """Hai Van Pass is geography AND it sits between Da Nang and Hue — it is
    not a section, it is something you stop at on the way to one."""
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _area_candidate(
                        "vn/hue/hai-van-pass",
                        "Hai Van Pass",
                        group="vn/da-nang>vn/hue",
                        progress=0.5,
                    )
                ],
            )
        ],
        [_area("vn/da-nang", "Da Nang"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    assert len(answer.items) == 1
    item = answer.items[0]
    assert item.kind == "area"
    assert item.group_key == "vn/da-nang>vn/hue"


def test_a_stretch_with_nothing_on_it_is_not_a_section() -> None:
    """A heading over empty space is noise. An empty AREA group survives —
    the area is itself the recommendation and its group carries the card."""
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [_candidate("A", "p1", source="suggested", group="vn/hoi-an")],
            )
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    assert [g.key for g in answer.groups] == ["vn/hoi-an", "vn/hue"]
    # Positions stay contiguous after the drop, so a client can index by them.
    assert [g.position for g in answer.groups] == [0, 1]


def test_a_stretch_that_has_stops_survives() -> None:
    answer = build_answer(
        [
            _result(
                "suggest_places",
                [
                    _candidate(
                        "On The Road",
                        "p1",
                        source="suggested",
                        group="vn/hoi-an>vn/hue",
                    )
                ],
            )
        ],
        [_area("vn/hoi-an", "Hoi An"), _area("vn/hue", "Hue")],
        journey=True,
    )
    assert answer is not None
    assert [g.key for g in answer.groups] == [
        "vn/hoi-an",
        "vn/hoi-an>vn/hue",
        "vn/hue",
    ]
