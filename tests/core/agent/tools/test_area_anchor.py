"""Area-anchored search scope — the geometry behind ADR-140 for areas.

The behaviours worth pinning here are the judgement calls, not the maths:
which extents are believed, what a bad extent degrades to, what gets dropped
when the fan-out is capped, and that only areas verified this turn can anchor
a search.
"""

from __future__ import annotations

from typing import Any

import pytest

from kebi.core.agent.tools._area_anchor import (
    anchor_context,
    anchor_radius_m,
    anchors_from_state,
    attribute_to_area,
    capped,
    gather_targets,
    place_on_journey,
    search_targets,
)
from kebi.core.areas.models import AreaEntity
from kebi.core.config import get_config


def _area(
    key: str = "vn/da-nang/an-thuong",
    name: str = "An Thuong",
    kind: str = "neighborhood",
    lat: float = 16.04,
    lng: float = 108.24,
    bbox: list[float] | None = None,
) -> AreaEntity:
    return AreaEntity(
        entity_key=key,
        entity_type=kind,  # type: ignore[arg-type]
        name=name,
        country_code="vn",
        lat=lat,
        lng=lng,
        bbox=bbox,
    )


def _state(anchors: list[AreaEntity]) -> dict[str, Any]:
    return {"area_anchors": [a.model_dump(mode="json") for a in anchors]}


def _movement() -> Any:
    return get_config().movement


# ---- extent trust ----------------------------------------------------------


def test_a_believable_extent_sets_the_radius() -> None:
    # ~0.02° of latitude ≈ 2.2 km — plausible for a neighborhood, so it is
    # used: half the span, so the disc covers the box.
    area = _area(bbox=[16.03, 16.05, 108.23, 108.25])
    assert anchor_radius_m(area, _movement()) == pytest.approx(1110, rel=0.05)


def test_an_implausibly_small_extent_degrades_to_the_kind_radius() -> None:
    """The Hai Van Pass case: Google returns a ~0.4 km box for a ~20 km road.

    Kebi has no better geometry for a linear feature and does not invent one —
    it stops believing the box and falls back to the kind's tier.
    """
    pass_area = _area(
        key="vn/da-nang/hai-van-pass",
        name="Hai Van Pass",
        kind="natural_feature",
        bbox=[16.199, 16.201, 108.129, 108.131],
    )
    radius = anchor_radius_m(pass_area, _movement())
    assert radius == _movement().radius_tiers.city


def test_no_extent_at_all_degrades_to_the_kind_radius() -> None:
    assert anchor_radius_m(_area(bbox=None), _movement()) == (
        _movement().radius_tiers.neighborhood
    )


def test_the_areas_own_name_rides_the_search_context() -> None:
    """The provider's reranker reads `city` as "which place is this about" —
    for a neighborhood or a pass, its own name beats the city containing it."""
    ctx = anchor_context(_area(), _movement())
    assert ctx.city == "An Thuong"
    assert (ctx.lat, ctx.lng) == (16.04, 108.24)


# ---- the cap ---------------------------------------------------------------


def test_cap_keeps_the_agents_order_and_drops_the_tail() -> None:
    areas = [_area(key=f"vn/c{i}", name=f"A{i}", kind="city") for i in range(6)]
    kept = capped(areas, 4)
    assert [a.name for a in kept] == ["A0", "A1", "A2", "A3"]


def test_under_the_cap_nothing_is_dropped() -> None:
    areas = [_area(key=f"vn/c{i}", name=f"A{i}", kind="city") for i in range(3)]
    assert capped(areas, 4) == areas


# ---- anchors from state ----------------------------------------------------


def test_no_keys_anchors_on_every_verified_area() -> None:
    areas = [_area(), _area(key="vn/hoi-an", name="Hoi An", kind="city")]
    assert [a.entity_key for a in anchors_from_state(_state(areas), None)] == [
        "vn/da-nang/an-thuong",
        "vn/hoi-an",
    ]


def test_keys_select_in_the_order_asked() -> None:
    areas = [_area(), _area(key="vn/hoi-an", name="Hoi An", kind="city")]
    picked = anchors_from_state(_state(areas), ["vn/hoi-an"])
    assert [a.entity_key for a in picked] == ["vn/hoi-an"]


def test_an_unverified_key_is_ignored_not_resolved() -> None:
    """A search inside an area has to stand on a verified footprint — an
    arbitrary key is not a second door into retrieval."""
    picked = anchors_from_state(_state([_area()]), ["vn/somewhere-made-up"])
    assert picked == []


def test_a_malformed_anchor_is_skipped_not_fatal() -> None:
    state = {"area_anchors": [{"nonsense": True}, _area().model_dump(mode="json")]}
    picked = anchors_from_state(state, None)  # type: ignore[arg-type]
    assert [a.name for a in picked] == ["An Thuong"]


# ---- attribution -----------------------------------------------------------


def test_a_point_is_attributed_to_the_nearest_containing_area() -> None:
    near = _area(key="vn/a", name="A", lat=16.04, lng=108.24)
    far = _area(key="vn/b", name="B", lat=16.08, lng=108.24)
    hit = attribute_to_area([far, near], _movement(), 16.041, 108.241)
    assert hit is not None and hit.name == "A"


def test_a_point_in_no_named_area_is_dropped() -> None:
    """The whole point of anchoring: a place clustered back at the origin is
    not an answer about the areas that were named."""
    area = _area(key="vn/a", name="A", lat=16.04, lng=108.24)
    assert attribute_to_area([area], _movement(), 21.03, 105.85) is None


# ---- fan-out ---------------------------------------------------------------


def _targets(anchors: list[AreaEntity], journey: bool = False) -> Any:
    return search_targets(anchors, _movement(), journey=journey, max_areas=4)


async def test_results_carry_where_they_came_from() -> None:
    a = _area(key="vn/a", name="A", kind="city", lat=16.0, lng=108.0)
    b = _area(key="vn/b", name="B", kind="city", lat=17.0, lng=109.0)

    async def _run(target: Any) -> list[str]:
        return [f"{target.group_key}-1"]

    tagged = await gather_targets(
        _targets([a, b]), concurrency=2, runner=_run, dedup_key=lambda s: s
    )
    assert [(t.group_key, item) for t, item in tagged] == [
        ("vn/a", "vn/a-1"),
        ("vn/b", "vn/b-1"),
    ]


async def test_dedup_keeps_the_first_target_that_surfaced_a_place() -> None:
    a = _area(key="vn/a", name="A", kind="city")
    b = _area(key="vn/b", name="B", kind="city")

    async def _run(_target: Any) -> list[str]:
        return ["shared"]

    tagged = await gather_targets(
        _targets([a, b]), concurrency=2, runner=_run, dedup_key=lambda s: s
    )
    assert [(t.group_key, item) for t, item in tagged] == [("vn/a", "shared")]


async def test_one_target_failing_does_not_lose_the_others() -> None:
    a = _area(key="vn/a", name="A", kind="city")
    b = _area(key="vn/b", name="B", kind="city")

    async def _run(target: Any) -> list[str]:
        if target.group_key == "vn/a":
            raise RuntimeError("provider down")
        return ["ok"]

    tagged = await gather_targets(
        _targets([a, b]), concurrency=2, runner=_run, dedup_key=lambda s: s
    )
    assert [(t.group_key, item) for t, item in tagged] == [("vn/b", "ok")]


async def test_every_target_failing_raises_for_the_caller_to_report() -> None:
    async def _run(_target: Any) -> list[str]:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await gather_targets(
            _targets([_area(key="vn/a", name="A", kind="city")]),
            concurrency=2,
            runner=_run,
            dedup_key=lambda s: s,
        )


# ---- journey: the areas as a path ------------------------------------------
#
# Whether there IS a path is the agent's call. These test what follows once it
# says so — including the regression that made this necessary: before, anchors
# suppressed the corridor entirely and a trip answer came back unfiltered.


def _hoi_an() -> AreaEntity:
    return _area(key="vn/hoi-an", name="Hoi An", kind="city", lat=15.88, lng=108.33)


def _hue() -> AreaEntity:
    return _area(key="vn/hue", name="Hue", kind="city", lat=16.46, lng=107.59)


def _hanoi() -> AreaEntity:
    return _area(key="vn/hanoi", name="Hanoi", kind="city", lat=21.03, lng=105.85)


def test_without_travel_between_there_is_no_road() -> None:
    """Two neighborhoods in one city have nothing between them worth pinning."""
    targets = search_targets(
        [_hoi_an(), _hue()], _movement(), journey=False, max_areas=4
    )
    assert [t.group_key for t in targets] == ["vn/hoi-an", "vn/hue"]


def test_a_journey_interleaves_the_road_in_travel_order() -> None:
    targets = search_targets(
        [_hoi_an(), _hue()], _movement(), journey=True, max_areas=4
    )
    keys = [t.group_key for t in targets]
    assert keys[0] == "vn/hoi-an"
    assert keys[-1] == "vn/hue"
    assert "vn/hoi-an>vn/hue" in keys
    # The stretch sits BETWEEN the two, not appended after them.
    assert keys.index("vn/hoi-an>vn/hue") < keys.index("vn/hue")


def test_an_undrivable_leg_contributes_no_stops() -> None:
    """Hanoi -> Hoi An is not a drive. Per ADR-138 the gate is per leg, so
    both endpoints are still searched — only the road between is skipped."""
    targets = search_targets(
        [_hanoi(), _hoi_an()], _movement(), journey=True, max_areas=4
    )
    assert [t.group_key for t in targets] == ["vn/hanoi", "vn/hoi-an"]


def test_a_mixed_trip_keeps_stops_on_the_drivable_leg_only() -> None:
    """The improvement over `route_too_long`, which suppressed everything."""
    targets = search_targets(
        [_hanoi(), _hoi_an(), _hue()], _movement(), journey=True, max_areas=6
    )
    stretches = {t.group_key for t in targets if t.area is None}
    assert "vn/hanoi>vn/hoi-an" not in stretches  # a flight
    assert "vn/hoi-an>vn/hue" in stretches  # the coastal ride


def test_a_place_inside_a_named_area_belongs_to_that_area() -> None:
    spot = place_on_journey([_hoi_an(), _hue()], _movement(), 15.881, 108.331)
    assert spot.on_journey
    assert spot.group_key == "vn/hoi-an"
    assert spot.area is not None


def test_a_place_on_the_road_belongs_to_the_stretch() -> None:
    """The case that makes a drive answerable: Lang Co is in neither city."""
    mid_lat = (15.88 + 16.46) / 2
    mid_lng = (108.33 + 107.59) / 2
    spot = place_on_journey([_hoi_an(), _hue()], _movement(), mid_lat, mid_lng)
    assert spot.on_journey
    assert spot.group_key == "vn/hoi-an>vn/hue"
    assert spot.area is None
    assert 0.0 < spot.progress < 1.0


def test_a_place_far_off_the_road_is_dropped() -> None:
    """THE regression guard: before, anchors bypassed the corridor entirely
    and a Da Nang -> Hue answer returned a park in Hoi An, the wrong way."""
    spot = place_on_journey([_hoi_an(), _hue()], _movement(), 21.03, 105.85)
    assert not spot.on_journey
    assert spot.group_key is None


def test_progress_orders_the_drive() -> None:
    early = place_on_journey([_hoi_an(), _hue()], _movement(), 15.90, 108.30)
    late = place_on_journey([_hoi_an(), _hue()], _movement(), 16.44, 107.62)
    assert early.progress < late.progress


def test_an_administrative_bbox_does_not_become_an_80km_search() -> None:
    """Vietnam's 2025 province merger left `vn/da-nang` with a 160 km bbox.
    Uncapped, that disc swallowed a whole Da Nang -> Hue ride and filed every
    stop under the origin — the failure anchoring exists to prevent."""
    da_nang = _area(
        key="vn/da-nang",
        name="Da Nang",
        kind="city",
        lat=16.05,
        lng=108.07,
        bbox=[14.95, 16.23, 107.21, 108.74],
    )
    assert anchor_radius_m(da_nang, _movement()) == 25_000.0


def test_the_bbox_still_travels_as_the_extent() -> None:
    """Capping reach must not rewrite geography — the boundary is true, and
    the client still shades it. Only the search radius is bounded."""
    from kebi.core.areas.models import AreaSummary

    da_nang = _area(
        key="vn/da-nang",
        name="Da Nang",
        kind="city",
        lat=16.05,
        lng=108.07,
        bbox=[14.95, 16.23, 107.21, 108.74],
    )
    assert AreaSummary.from_entity(da_nang).extent == [14.95, 16.23, 107.21, 108.74]


def test_a_named_area_wins_over_the_road_when_it_contains_the_point() -> None:
    """Attribution favours the endpoints, and this documents the limit rather
    than pretending otherwise: Hai Van Pass sits 18 km from Da Nang's centre
    and files under Da Nang, not under the ride.

    Attributing by a tight radius instead was tried and broke worse —
    post-merger Vietnamese city rows carry PROVINCE centroids, so Hue's own
    Imperial City is 16 km from the stored "Hue" point and the city's
    landmarks came back as "on the way".
    """
    da_nang = _area(
        key="vn/da-nang",
        name="Da Nang",
        kind="city",
        lat=16.05,
        lng=108.07,
        bbox=[14.95, 16.23, 107.21, 108.74],
    )
    hue = _area(
        key="vn/hue",
        name="Hue",
        kind="city",
        lat=16.35,
        lng=107.48,
        bbox=[15.99, 16.74, 107.03, 108.19],
    )
    spot = place_on_journey([da_nang, hue], _movement(), 16.2, 108.133)
    assert spot.on_journey
    assert spot.group_key == "vn/da-nang"
