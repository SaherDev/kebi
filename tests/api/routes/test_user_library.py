"""Tests for GET /v1/user/library (the Library browse endpoint)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_place_notes_service,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.user import router as user_router
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

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _make_app(service: AsyncMock, notes_service: AsyncMock | None = None) -> TestClient:
    notes_service = notes_service or AsyncMock(
        notes_for_saves=AsyncMock(return_value={})
    )
    app = FastAPI()
    register_error_handlers(app)  # wires ValueError → 400 + X-Request-Id
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_places_service] = lambda: service
    app.dependency_overrides[get_place_notes_service] = lambda: notes_service
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
    service.browse = AsyncMock(return_value=([_view("p1")], "next-tok", 1))
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


def test_empty_library_returns_empty_state(svc: AsyncMock) -> None:
    svc.browse = AsyncMock(return_value=([], None, 0))
    client = _make_app(svc)

    resp = client.get("/v1/user/library")

    assert resp.status_code == 200
    assert resp.json() == {"places": [], "next_cursor": None, "total": 0}


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
