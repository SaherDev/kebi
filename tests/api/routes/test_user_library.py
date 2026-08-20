"""Tests for GET /v1/user/library (the Library browse endpoint)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_area_handle_builder,
    get_library_areas_service,
    get_place_notes_service,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.user import router as user_router
from kebi.core.areas.handles import AreaHandle, AreaHandleBuilder, AreaRef
from kebi.core.areas.library_areas_service import AreaWithCount, LibraryAreaIndex
from kebi.core.knowledge.schemas import PlaceNote
from kebi.core.places import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
    UserPlacesService,
)
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_TEST_USER_ID = "user_test_dummy_123456789012345"

# Geo keys are registry id-paths now; library rows are keyed by the place's
# stored `geo_key`, and unprofiled areas are named from their registry row.
_BALI = make_city("id", "Bali")
_CANGGU = make_area(_BALI, "Canggu")


def _make_app(
    service: AsyncMock,
    notes_service: AsyncMock | None = None,
    areas_service: AsyncMock | None = None,
) -> TestClient:
    notes_service = notes_service or AsyncMock(
        notes_for_saves=AsyncMock(return_value={})
    )
    app = FastAPI()
    register_error_handlers(app)  # wires ValueError → 400 + X-Request-Id
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_places_service] = lambda: service
    app.dependency_overrides[get_place_notes_service] = lambda: notes_service
    app.dependency_overrides[get_area_handle_builder] = lambda: AreaHandleBuilder(
        area_repo=MagicMock(get_many=AsyncMock(return_value={})),
        geo_registry=FakeGeoRegistry(_BALI, _CANGGU),
    )
    app.dependency_overrides[get_library_areas_service] = lambda: (
        areas_service
        or AsyncMock(list_areas=AsyncMock(return_value=LibraryAreaIndex()))
    )
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    return TestClient(app)


def _view(pid: str) -> SavedPlaceView:
    return SavedPlaceView(
        place=PlaceCore(id=pid, place_name=f"Place {pid}", location=LocationContext()),
        user_data=UserPlace(
            user_place_id=f"up-{pid}",
            user_id=_TEST_USER_ID,
            place_id=pid,
            source=PlaceSource.manual,
            saved_at=datetime(2026, 6, 9, tzinfo=UTC),
        ),
    )


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=UserPlacesService)
    service.browse = AsyncMock(return_value=([_view("p1")], "next-tok", 1, 1))
    return service


def test_returns_places_and_next_cursor(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library")

    assert resp.status_code == 200
    body = resp.json()
    assert [p["place"]["id"] for p in body["places"]] == ["p1"]
    assert body["next_cursor"] == "next-tok"
    assert body["total"] == 1

    # user_id from the gateway identity; defaults: limit 50, no cursor.
    args = svc.browse.await_args.args
    kwargs = svc.browse.await_args.kwargs
    assert args[0] == _TEST_USER_ID
    assert args[2] == 50  # limit
    assert kwargs["cursor"] is None


def test_insider_notes_surface_on_each_place(svc: AsyncMock) -> None:
    """A place's claims appear as `claims` on its item, projected to the public
    note shape (coarse source label + from_shared, no raw source_ref)."""
    notes_service = AsyncMock(
        notes_for_saves=AsyncMock(
            return_value={
                "p1": [
                    PlaceNote(
                        id="claim-1",
                        text="order the omakase",
                        tags=["food"],
                        source_type="shared_content",
                        from_shared=True,
                        agree_count=3,
                        disagree_count=1,
                    )
                ]
            }
        )
    )
    client = _make_app(svc, notes_service)

    body = client.get("/v1/user/library").json()

    claims = body["places"][0]["claims"]
    assert len(claims) == 1
    assert claims[0] == {
        "id": "claim-1",
        "text": "order the omakase",
        "tags": ["food"],
        "source": "community",
        "from_shared": True,
        "agree_count": 3,
        "disagree_count": 1,
    }


def test_place_without_claims_has_empty_list(svc: AsyncMock) -> None:
    client = _make_app(svc)  # default notes service returns {}

    body = client.get("/v1/user/library").json()

    assert body["places"][0]["claims"] == []


def test_user_id_is_not_exposed_in_response(svc: AsyncMock) -> None:
    # Security: the caller's identity must not be echoed back in the payload.
    client = _make_app(svc)

    body = client.get("/v1/user/library").json()

    user_data = body["places"][0]["user_data"]
    assert "user_id" not in user_data
    # the fields the Library screen needs are still present
    assert user_data["user_place_id"] == "up-p1"
    assert {"visited", "liked", "note", "source", "source_label", "saved_at"} <= (
        user_data.keys()
    )


def test_filters_and_paging_passed_through(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get(
        "/v1/user/library",
        params={
            "category": "cafe",
            "visited": "false",
            "source": "tiktok",
            "city": "Bangkok",
            "limit": "10",
            "cursor": "abc",
        },
    )

    assert resp.status_code == 200
    args = svc.browse.await_args.args
    kwargs = svc.browse.await_args.kwargs
    filters = args[1]
    assert filters.categories == [PlaceCategory.cafe]
    assert filters.visited is False
    assert filters.source == PlaceSource.tiktok
    assert filters.city == "Bangkok"
    assert args[2] == 10  # limit
    assert kwargs["cursor"] == "abc"


def test_q_reaches_the_service_as_the_search_predicate(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"q": "cang"})

    assert resp.status_code == 200
    assert svc.browse.await_args.args[1].query == "cang"


def test_q_combines_with_other_filters(svc: AsyncMock) -> None:
    """ANDed, not replacing: searching inside an already-filtered view must
    narrow it further rather than reset it."""
    client = _make_app(svc)

    resp = client.get(
        "/v1/user/library", params={"q": "sushi", "category": "cafe", "visited": "true"}
    )

    assert resp.status_code == 200
    filters = svc.browse.await_args.args[1]
    assert filters.query == "sushi"
    assert filters.categories == [PlaceCategory.cafe]
    assert filters.visited is True


def test_filtered_total_is_the_whole_match_set_not_the_page(svc: AsyncMock) -> None:
    """`3 of 84` — the 3 counts every match in the library, not the rows on
    this page. A client cannot compute this itself, which is the point."""
    svc.browse = AsyncMock(return_value=([_view("p1")], "next-tok", 84, 3))
    client = _make_app(svc)

    body = client.get("/v1/user/library", params={"q": "sushi"}).json()

    assert len(body["places"]) == 1
    assert body["filtered_total"] == 3
    assert body["total"] == 84


def test_no_search_makes_the_two_counts_agree(svc: AsyncMock) -> None:
    svc.browse = AsyncMock(return_value=([_view("p1")], None, 84, 84))
    client = _make_app(svc)

    body = client.get("/v1/user/library").json()

    assert body["filtered_total"] == body["total"] == 84


def test_q_over_length_cap_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"q": "x" * 201})

    assert resp.status_code == 422


def test_empty_library_returns_empty_state(svc: AsyncMock) -> None:
    svc.browse = AsyncMock(return_value=([], None, 0, 0))
    client = _make_app(svc)

    resp = client.get("/v1/user/library")

    assert resp.status_code == 200
    assert resp.json() == {
        "places": [],
        "next_cursor": None,
        "total": 0,
        "filtered_total": 0,
    }


def test_unknown_query_param_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"bogus": "1"})

    assert resp.status_code == 422


def test_limit_over_cap_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"limit": "1000"})

    assert resp.status_code == 422


def test_malformed_cursor_returns_400(svc: AsyncMock) -> None:
    # The service raises ValueError on a bad cursor; the shared handler → 400.
    svc.browse = AsyncMock(side_effect=ValueError("invalid library cursor: '@@'"))
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"cursor": "@@"})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Areas on rows, the ?area= filter, and the area index (ADR-165)
# ---------------------------------------------------------------------------


def _bali_view(pid: str) -> SavedPlaceView:
    view = _view(pid)
    view.place.location = LocationContext(
        country_code="id", city="Bali", neighborhood="Canggu"
    )
    # The row's area handle is keyed by the place's STORED registry key.
    view.place.geo_key = _CANGGU.geo_key
    return view


def test_row_carries_a_tappable_area(svc: AsyncMock) -> None:
    """The blocker the client named: rows had display strings but no handle,
    so a group heading could not be a link."""
    svc.browse = AsyncMock(return_value=([_bali_view("p1")], None, 1, 1))
    client = _make_app(svc)

    area = client.get("/v1/user/library").json()["places"][0]["area"]

    assert area["key"] == _CANGGU.geo_key
    assert area["name"] == "Canggu"
    assert area["uri"].startswith("kebi://area/")
    assert area["parent"]["key"] == _BALI.geo_key


def test_area_is_top_level_not_nested_in_location(svc: AsyncMock) -> None:
    """Deliberately a sibling of `place`: the URI and icon are wire and
    areas-table concerns, not properties of a stored location (ADR-105)."""
    svc.browse = AsyncMock(return_value=([_bali_view("p1")], None, 1, 1))
    client = _make_app(svc)

    item = client.get("/v1/user/library").json()["places"][0]

    assert "area" in item
    assert "area" not in item["place"]["location"]


def test_place_without_a_city_has_no_area(svc: AsyncMock) -> None:
    """Null means "geography coarser than a city" — the client's `elsewhere`
    bucket — not "this area has no profile"."""
    client = _make_app(svc)  # default _view has an empty LocationContext

    assert client.get("/v1/user/library").json()["places"][0]["area"] is None


def test_area_filter_reaches_the_service(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"area": _CANGGU.geo_key})

    assert resp.status_code == 200
    assert svc.browse.await_args.args[1].area == _CANGGU.geo_key


def test_area_filter_combines_with_search(svc: AsyncMock) -> None:
    """Task A and Task B compose: searching inside one area narrows, never
    resets."""
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"area": _BALI.geo_key, "q": "warung"})

    assert resp.status_code == 200
    filters = svc.browse.await_args.args[1]
    assert (filters.area, filters.query) == (_BALI.geo_key, "warung")


def test_malformed_area_key_rejected_422(svc: AsyncMock) -> None:
    """Loud, not silent: a typo'd key matching nothing is indistinguishable
    from "you have no saves here"."""
    client = _make_app(svc)

    resp = client.get("/v1/user/library", params={"area": "not a key!"})

    assert resp.status_code == 422


def test_area_index_returns_handles_with_exact_counts(svc: AsyncMock) -> None:
    areas_service = AsyncMock(
        list_areas=AsyncMock(
            return_value=LibraryAreaIndex(
                areas=[
                    AreaWithCount(
                        area=AreaHandle(
                            key=_CANGGU.geo_key,
                            name="Canggu",
                            uri="kebi://area/abc",
                            icon="🏄",
                            parent=AreaRef(
                                key=_BALI.geo_key, name="Bali", uri="kebi://area/def"
                            ),
                        ),
                        count=11,
                    )
                ],
                unassigned=3,
            )
        )
    )
    client = _make_app(svc, areas_service=areas_service)

    body = client.get("/v1/user/library/areas").json()

    assert body == {
        "areas": [
            {
                "area": {
                    "key": _CANGGU.geo_key,
                    "name": "Canggu",
                    "uri": "kebi://area/abc",
                    "icon": "🏄",
                    "country_code": "id",
                    "parent": {
                        "key": _BALI.geo_key,
                        "name": "Bali",
                        "uri": "kebi://area/def",
                        "icon": None,
                        "country_code": "id",
                    },
                },
                "count": 11,
            }
        ],
        "unassigned_count": 3,
    }
    areas_service.list_areas.assert_awaited_once_with(_TEST_USER_ID)


def test_elsewhere_count_is_served_not_derived(svc: AsyncMock) -> None:
    """The client's "elsewhere" heading gets a number from kebi. Derived as
    `total` minus the distribution it is wrong until the whole library is
    paged in; served, it is right on first paint."""
    areas_service = AsyncMock(
        list_areas=AsyncMock(return_value=LibraryAreaIndex(areas=[], unassigned=7))
    )
    client = _make_app(svc, areas_service=areas_service)

    assert client.get("/v1/user/library/areas").json() == {
        "areas": [],
        "unassigned_count": 7,
    }


def test_area_index_ignores_search_and_filters(svc: AsyncMock) -> None:
    """The at-rest index: an index that narrowed while someone typed would
    shift the section list under them."""
    areas_service = AsyncMock(list_areas=AsyncMock(return_value=LibraryAreaIndex()))
    client = _make_app(svc, areas_service=areas_service)

    resp = client.get("/v1/user/library/areas", params={"q": "sushi"})

    assert resp.status_code == 200
    # `q` is not a param here at all — it takes only the caller's identity.
    areas_service.list_areas.assert_awaited_once_with(_TEST_USER_ID)


def test_empty_library_has_an_empty_area_index(svc: AsyncMock) -> None:
    client = _make_app(svc)

    assert client.get("/v1/user/library/areas").json() == {
        "areas": [],
        "unassigned_count": 0,
    }
