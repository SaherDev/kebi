"""Tests for the catalog floor (ADR-140).

This behaviour used to be the `discover_places` tool and carried its coverage;
the tool is gone and the search now runs automatically inside
`suggest_places`, so the coverage moves here with it. What is under test is a
*safety* property: when nothing was named and nothing validated, the answer
still contains real nearby venues rather than an invented tip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools._catalog_fallback import catalog_candidates
from kebi.core.places.models import PlaceCategory, PlaceObject, PlaceTag
from kebi.core.places.tags import DietaryTag, TagType


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Thailand",
        "city": "Bangkok",
        "neighborhood": "Sukhumvit",
        "lat": 13.7563,
        "lng": 100.5018,
        "density": "dense",
        "effective_mode": "walking",
        "scope_tier": "walkable",
        "scope_shape": "area",
        "search_radius_m": 1200.0,
    }
    base.update(kw)
    return WorkingLocation(**base)


def _place(name: str, *, place_id: str, tags: list[PlaceTag] | None = None):
    return PlaceObject(
        id=place_id,
        provider_id=f"google:{place_id}",
        place_name=name,
        categories=[PlaceCategory.restaurant],
        tags=tags or [],
        cached_at=datetime.now(UTC),
    )


def _veg_tag() -> PlaceTag:
    return PlaceTag(
        type=TagType.dietary, value=DietaryTag.vegetarian.value, source="google"
    )


def _factory(
    hits: list[Any] | None = None, *, raises: Exception | None = None
) -> tuple[Callable[[], AbstractAsyncContextManager[MagicMock]], MagicMock]:
    svc = MagicMock()
    svc.find = (
        AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=hits or [])
    )

    @asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield svc

    return factory, svc


async def _run(factory: Any, **kw: Any) -> list[Any]:
    return await catalog_candidates(
        places_search_factory=factory,
        working=kw.pop("working", _working()),
        query=kw.pop("query", "cafe"),
        categories=kw.pop("categories", None),
        tags=kw.pop("tags", None),
        limit=kw.pop("limit", 5),
    )


async def test_provider_hits_become_discovered_candidates() -> None:
    factory, _svc = _factory([_place("Nara", place_id="p1")])
    found = await _run(factory)
    assert [c.place.place_name for c in found] == ["Nara"]
    assert found[0].source == "discovered"
    assert found[0].user_data is None


async def test_the_search_is_anchored_on_the_turns_circle() -> None:
    factory, svc = _factory([])
    await _run(factory, working=_working(search_radius_m=1200.0))
    query = svc.find.await_args.args[0]
    assert query.location.lat == 13.7563
    assert query.location.lng == 100.5018
    assert query.location.radius_m == 1200


async def test_no_radius_means_no_provider_call() -> None:
    # Nothing to anchor a nearby search on — spending a paid lookup would be
    # guessing, so the floor declines rather than searching the wrong place.
    factory, svc = _factory([_place("Nara", place_id="p1")])
    assert await _run(factory, working=_working(search_radius_m=0.0)) == []
    svc.find.assert_not_awaited()


async def test_safety_tags_exclude_a_violating_place() -> None:
    factory, _svc = _factory(
        [
            _place("Steakhouse", place_id="p1"),
            _place("Green", place_id="p2", tags=[_veg_tag()]),
        ]
    )
    found = await _run(factory, tags=["vegetarian"])
    assert [c.place.place_name for c in found] == ["Green"]


async def test_a_soft_tag_never_excludes_an_untagged_place() -> None:
    # A fresh catalog row simply has no experiential tags yet; absence there is
    # ignorance, not "no" (ADR-118).
    factory, _svc = _factory([_place("Nara", place_id="p1")])
    found = await _run(factory, tags=["cozy", "rooftop"])
    assert [c.place.place_name for c in found] == ["Nara"]


async def test_limit_caps_the_list() -> None:
    factory, _svc = _factory([_place(f"P{i}", place_id=f"p{i}") for i in range(8)])
    assert len(await _run(factory, limit=3)) == 3


async def test_a_provider_failure_returns_nothing_rather_than_raising() -> None:
    """The floor runs *after* the caller already failed to find anything.

    Letting an exception through would turn a thin answer into no answer,
    which is the wrong trade for a fallback.
    """
    factory, _svc = _factory(raises=RuntimeError("provider down"))
    assert await _run(factory) == []


async def test_empty_provider_response_is_an_honest_empty() -> None:
    factory, _svc = _factory([])
    assert await _run(factory) == []
