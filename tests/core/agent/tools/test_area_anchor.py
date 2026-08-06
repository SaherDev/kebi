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
    gather_per_area,
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


async def test_results_carry_the_area_they_came_from() -> None:
    a = _area(key="vn/a", name="A", kind="city", lat=16.0, lng=108.0)
    b = _area(key="vn/b", name="B", kind="city", lat=17.0, lng=109.0)

    async def _run(entity: AreaEntity, _ctx: Any) -> list[str]:
        return [f"{entity.name}-1"]

    tagged = await gather_per_area(
        [a, b],
        _movement(),
        max_areas=4,
        concurrency=2,
        runner=_run,
        dedup_key=lambda s: s,
    )
    assert [(area.name, item) for area, item in tagged] == [("A", "A-1"), ("B", "B-1")]


async def test_dedup_keeps_the_first_area_that_surfaced_a_place() -> None:
    a = _area(key="vn/a", name="A", kind="city")
    b = _area(key="vn/b", name="B", kind="city")

    async def _run(_entity: AreaEntity, _ctx: Any) -> list[str]:
        return ["shared"]

    tagged = await gather_per_area(
        [a, b],
        _movement(),
        max_areas=4,
        concurrency=2,
        runner=_run,
        dedup_key=lambda s: s,
    )
    assert [(area.name, item) for area, item in tagged] == [("A", "shared")]


async def test_one_area_failing_does_not_lose_the_others() -> None:
    a = _area(key="vn/a", name="A", kind="city")
    b = _area(key="vn/b", name="B", kind="city")

    async def _run(entity: AreaEntity, _ctx: Any) -> list[str]:
        if entity.name == "A":
            raise RuntimeError("provider down")
        return ["ok"]

    tagged = await gather_per_area(
        [a, b],
        _movement(),
        max_areas=4,
        concurrency=2,
        runner=_run,
        dedup_key=lambda s: s,
    )
    assert [(area.name, item) for area, item in tagged] == [("B", "ok")]


async def test_every_area_failing_raises_for_the_caller_to_report() -> None:
    async def _run(_entity: AreaEntity, _ctx: Any) -> list[str]:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await gather_per_area(
            [_area(key="vn/a", name="A", kind="city")],
            _movement(),
            max_areas=4,
            concurrency=2,
            runner=_run,
            dedup_key=lambda s: s,
        )
