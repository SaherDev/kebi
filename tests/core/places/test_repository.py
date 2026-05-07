"""Unit tests for PlacesRepository (ADR-054, feature 019).

All DB access is mocked — these tests verify behavior around batching,
duplicate-detection error wrapping, and ORM→PlaceObject materialization,
not SQL correctness.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from kebi.core.places.models import (
    DuplicatePlaceError,
    PlaceAttributes,
    PlaceCreate,
    PlaceProvider,
    PlaceSource,
    PlaceType,
)
from kebi.core.places.repository import PlacesRepository

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _row_mapping(
    *,
    id: str = "pid-1",
    user_id: str = "u1",
    place_name: str = "Cafe A",
    place_type: str = "food_and_drink",
    subcategory: str | None = "cafe",
    tags: list[str] | None = None,
    attributes: dict[str, Any] | None = None,
    source_url: str | None = None,
    source: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "user_id": user_id,
        "place_name": place_name,
        "place_type": place_type,
        "subcategory": subcategory,
        "tags": tags,
        "attributes": attributes,
        "source_url": source_url,
        "source": source,
        "provider_id": provider_id,
        "created_at": None,
    }


def _returning_result(rows: list[dict[str, Any]]) -> MagicMock:
    """Build a mock sqlalchemy Result where .all() returns row objects
    whose ._mapping is the supplied dict, and .one() returns the single row."""
    row_objs = [SimpleNamespace(_mapping=r) for r in rows]
    result = MagicMock()
    result.all.return_value = row_objs
    if len(row_objs) == 1:
        result.one.return_value = row_objs[0]
    return result


# Default valid subcategory per PlaceType so parametrized tests over all
# five types pick a value that passes the per-type subcategory vocabulary check.
_DEFAULT_SUBCATEGORY_BY_TYPE: dict[PlaceType, str] = {
    PlaceType.food_and_drink: "cafe",
    PlaceType.things_to_do: "museum",
    PlaceType.shopping: "bookstore",
    PlaceType.services: "coworking",
    PlaceType.accommodation: "hotel",
}


def _make_place_create(
    place_name: str = "Cafe A",
    provider: PlaceProvider | None = PlaceProvider.google,
    external_id: str | None = "ChIJ_aaa",
    subcategory: str | None = None,
    place_type: PlaceType = PlaceType.food_and_drink,
) -> PlaceCreate:
    attrs_kwargs: dict[str, Any] = {"price_hint": "moderate"}
    if place_type == PlaceType.food_and_drink:
        attrs_kwargs["cuisine"] = "japanese"
    return PlaceCreate(
        user_id="u1",
        place_name=place_name,
        place_type=place_type,
        subcategory=(
            subcategory
            if subcategory is not None
            else _DEFAULT_SUBCATEGORY_BY_TYPE[place_type]
        ),
        tags=["hidden-gem"],
        attributes=PlaceAttributes(**attrs_kwargs),
        source=PlaceSource.manual,
        provider=provider,
        external_id=external_id,
    )


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# _build_provider_id
# ---------------------------------------------------------------------------


def test_build_provider_id_joins_provider_and_external_id() -> None:
    assert (
        PlacesRepository._build_provider_id(PlaceProvider.google, "ChIJ_xxx")
        == "google:ChIJ_xxx"
    )


def test_build_provider_id_with_none_provider_returns_none() -> None:
    assert PlacesRepository._build_provider_id(None, "ChIJ_xxx") is None


def test_build_provider_id_with_none_external_id_returns_none() -> None:
    assert PlacesRepository._build_provider_id(PlaceProvider.google, None) is None


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("place_type", list(PlaceType))
async def test_create_inserts_namespaced_provider_id(place_type: PlaceType) -> None:
    session = _mock_session()
    session.execute.return_value = _returning_result(
        [
            _row_mapping(
                provider_id="google:ChIJ_aaa",
                place_type=place_type.value,
            )
        ]
    )
    repo = PlacesRepository(session)

    place = await repo.create(_make_place_create(place_type=place_type))

    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert place.provider_id == "google:ChIJ_aaa"
    assert place.place_name == "Cafe A"
    assert place.place_type == place_type
    assert place.geo_fresh is False
    assert place.enriched is False


async def test_create_without_provider_stores_null_provider_id() -> None:
    session = _mock_session()
    session.execute.return_value = _returning_result([_row_mapping(provider_id=None)])
    repo = PlacesRepository(session)

    data = _make_place_create(provider=None, external_id=None)
    place = await repo.create(data)

    assert place.provider_id is None


async def test_create_integrity_error_raises_duplicate_place_error() -> None:
    session = _mock_session()

    # First execute() raises IntegrityError (the INSERT).
    # Second execute() (the lookup in _lookup_place_id_by_provider_id)
    # returns the existing place_id.
    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none.return_value = "existing-pid"
    session.execute.side_effect = [
        IntegrityError("unique violation", params=None, orig=Exception()),
        lookup_result,
    ]
    repo = PlacesRepository(session)

    with pytest.raises(DuplicatePlaceError) as exc_info:
        await repo.create(_make_place_create())

    session.rollback.assert_awaited()
    assert len(exc_info.value.conflicts) == 1
    conflict = exc_info.value.conflicts[0]
    assert conflict.provider_id == "google:ChIJ_aaa"
    assert conflict.existing_place_id == "existing-pid"


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------


async def test_create_batch_empty_returns_empty_without_db_call() -> None:
    session = _mock_session()
    repo = PlacesRepository(session)

    result = await repo.create_batch([])

    assert result == []
    session.execute.assert_not_awaited()


async def test_create_batch_three_items_issues_exactly_one_execute() -> None:
    session = _mock_session()
    inputs = [
        _make_place_create(place_name="A", external_id="id_a"),
        _make_place_create(place_name="B", external_id="id_b"),
        _make_place_create(place_name="C", external_id="id_c"),
    ]
    returned = _returning_result(
        [
            _row_mapping(id="p_a", place_name="A", provider_id="google:id_a"),
            _row_mapping(id="p_b", place_name="B", provider_id="google:id_b"),
            _row_mapping(id="p_c", place_name="C", provider_id="google:id_c"),
        ]
    )
    session.execute.return_value = returned
    repo = PlacesRepository(session)

    result = await repo.create_batch(inputs)

    assert session.execute.await_count == 1
    assert [p.place_name for p in result] == ["A", "B", "C"]
    assert [p.place_id for p in result] == ["p_a", "p_b", "p_c"]


async def test_create_batch_collision_rolls_back_and_raises_duplicate() -> None:
    session = _mock_session()
    inputs = [
        _make_place_create(place_name="A", external_id="id_a"),
        _make_place_create(place_name="B", external_id="id_b"),
    ]

    def lookup_side_effect(*_args: Any, **_kwargs: Any) -> MagicMock:
        m = MagicMock()
        # both provider_ids already exist → both returned as conflicts
        m.scalar_one_or_none.return_value = "existing-pid"
        return m

    session.execute.side_effect = [
        IntegrityError("unique violation", params=None, orig=Exception()),
        lookup_side_effect(),
        lookup_side_effect(),
    ]
    repo = PlacesRepository(session)

    with pytest.raises(DuplicatePlaceError) as exc_info:
        await repo.create_batch(inputs)

    session.rollback.assert_awaited()
    assert len(exc_info.value.conflicts) == 2
    assert {c.provider_id for c in exc_info.value.conflicts} == {
        "google:id_a",
        "google:id_b",
    }


# ---------------------------------------------------------------------------
# get_by_external_id
# ---------------------------------------------------------------------------


async def test_get_by_external_id_queries_namespaced_provider_id() -> None:
    session = _mock_session()

    # Mock .scalar_one_or_none() to return a fake ORM row
    orm_row = SimpleNamespace(
        id="pid-42",
        user_id="u1",
        place_name="Cafe A",
        place_type="food_and_drink",
        subcategory="cafe",
        tags=None,
        attributes=None,
        source_url=None,
        source=None,
        provider_id="google:ChIJ_aaa",
        created_at=None,
    )
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = orm_row
    session.execute.return_value = result_mock
    repo = PlacesRepository(session)

    place = await repo.get_by_external_id(PlaceProvider.google, "ChIJ_aaa")

    session.execute.assert_awaited_once()
    assert place is not None
    assert place.provider_id == "google:ChIJ_aaa"
    assert place.place_id == "pid-42"


async def test_get_by_external_id_returns_none_when_not_found() -> None:
    session = _mock_session()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute.return_value = result_mock
    repo = PlacesRepository(session)

    place = await repo.get_by_external_id(PlaceProvider.google, "ChIJ_nope")

    assert place is None


# ---------------------------------------------------------------------------
# get / get_batch
# ---------------------------------------------------------------------------


async def test_get_returns_place_object_with_tier1_fields() -> None:
    session = _mock_session()
    orm_row = SimpleNamespace(
        id="pid-1",
        user_id="u1",
        place_name="Cafe A",
        place_type="food_and_drink",
        subcategory="cafe",
        tags=["hidden-gem"],
        attributes={"cuisine": "japanese"},
        source_url=None,
        source="manual",
        provider_id="google:abc",
        created_at=None,
    )
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = orm_row
    session.execute.return_value = result_mock
    repo = PlacesRepository(session)

    place = await repo.get("pid-1")

    assert place is not None
    assert place.place_id == "pid-1"
    assert place.attributes.cuisine == "japanese"
    assert place.tags == ["hidden-gem"]
    assert place.geo_fresh is False
    assert place.enriched is False


async def test_get_batch_empty_returns_empty_without_db_call() -> None:
    session = _mock_session()
    repo = PlacesRepository(session)

    result = await repo.get_batch([])

    assert result == []
    session.execute.assert_not_awaited()


async def test_get_batch_preserves_input_order_and_omits_missing() -> None:
    session = _mock_session()
    # Request [a, b, c] — DB returns [c, a] (no b)
    rows = [
        SimpleNamespace(
            id="a",
            user_id="u1",
            place_name="A",
            place_type="food_and_drink",
            subcategory=None,
            tags=None,
            attributes=None,
            source_url=None,
            source=None,
            provider_id=None,
            created_at=None,
        ),
        SimpleNamespace(
            id="c",
            user_id="u1",
            place_name="C",
            place_type="food_and_drink",
            subcategory=None,
            tags=None,
            attributes=None,
            source_url=None,
            source=None,
            provider_id=None,
            created_at=None,
        ),
    ]
    scalar_result = MagicMock()
    scalar_result.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalar_result
    session.execute.return_value = result_mock
    repo = PlacesRepository(session)

    result = await repo.get_batch(["a", "b", "c"])

    assert [p.place_id for p in result] == ["a", "c"]
