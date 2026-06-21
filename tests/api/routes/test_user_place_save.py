"""Tests for POST /v1/user/places — the consult card's "save it" action.

The route's contract: link a recommended catalog place to the caller
(`source=kebi`), emit a `RecommendationSaved` positive taste signal on a
genuinely new save (and *not* on an idempotent re-tap), identity from the
gateway never the body, the created user-state DTO on success (never the raw
domain model — ADR-105), and a 404 when the place is not in the catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_event_dispatcher,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.routes.user import router as user_router
from kebi.core.events.events import RecommendationSaved
from kebi.core.places import (
    PlaceNotFoundError,
    PlaceSource,
    UserPlace,
    UserPlacesService,
)

_TEST_USER_ID = "user_test_dummy_123456789012345"
_REC_ID = "rec-abc-123"


def _body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "place_core_id": "p1",
        "recommendation_id": _REC_ID,
    }
    base.update(overrides)
    return base


def _saved(**overrides: object) -> UserPlace:
    base = {
        "user_place_id": "up-1",
        "user_id": _TEST_USER_ID,
        "place_id": "p1",
        "approved": False,
        "source": PlaceSource.kebi,
        "saved_at": datetime(2026, 6, 9, tzinfo=UTC),
    }
    base.update(overrides)
    return UserPlace(**base)  # type: ignore[arg-type]


def _make_app(service: UserPlacesService, dispatcher: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_places_service] = lambda: service
    app.dependency_overrides[get_event_dispatcher] = lambda: dispatcher
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def dispatcher() -> AsyncMock:
    return AsyncMock(dispatch=AsyncMock())


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=UserPlacesService)
    service.save_one = AsyncMock(return_value=(_saved(), True))
    return service


def test_save_returns_201_and_user_state_dto(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    client = _make_app(svc, dispatcher)

    response = client.post("/v1/user/places", json=_body())

    assert response.status_code == 201
    body = response.json()
    assert body["user_place_id"] == "up-1"
    assert body["place_id"] == "p1"
    assert body["source"] == "kebi"
    assert body["approved"] is False


def test_save_response_omits_user_id(svc: AsyncMock, dispatcher: AsyncMock) -> None:
    """ADR-105 — the caller's identity is never echoed back in the payload."""
    client = _make_app(svc, dispatcher)

    response = client.post("/v1/user/places", json=_body())

    assert response.status_code == 201
    assert "user_id" not in response.json()


def test_save_stamps_kebi_source_and_gateway_identity(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    """source is server-set to kebi; user_id comes from the gateway, not body."""
    client = _make_app(svc, dispatcher)

    client.post("/v1/user/places", json=_body())

    user_id, place_id, source = svc.save_one.await_args.args
    assert user_id == _TEST_USER_ID
    assert place_id == "p1"
    assert source is PlaceSource.kebi


def test_save_dispatches_recommendation_saved_signal_on_new_save(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    """A new save emits exactly one RecommendationSaved carrying the
    recommendation it came from and the saved place."""
    client = _make_app(svc, dispatcher)

    client.post("/v1/user/places", json=_body())

    dispatcher.dispatch.assert_awaited_once()
    event = dispatcher.dispatch.await_args.args[0]
    assert isinstance(event, RecommendationSaved)
    assert event.user_id == _TEST_USER_ID
    assert event.recommendation_id == _REC_ID
    assert event.place_core_id == "p1"


def test_resave_is_idempotent_and_skips_signal(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    """A re-tap (created=False) returns 201 with the existing save but does
    NOT re-emit the taste signal — saving twice never double-trains taste."""
    svc.save_one = AsyncMock(return_value=(_saved(), False))
    client = _make_app(svc, dispatcher)

    response = client.post("/v1/user/places", json=_body())

    assert response.status_code == 201
    dispatcher.dispatch.assert_not_awaited()


def test_save_unknown_place_returns_404(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    """place_core_id absent from the catalog → 404, no signal dispatched."""
    svc.save_one = AsyncMock(side_effect=PlaceNotFoundError("ghost"))
    client = _make_app(svc, dispatcher)

    response = client.post(
        "/v1/user/places", json=_body(place_core_id="ghost")
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "place_not_found"
    dispatcher.dispatch.assert_not_awaited()


def test_save_unknown_field_returns_422(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    client = _make_app(svc, dispatcher)

    response = client.post("/v1/user/places", json=_body(bogus=True))

    assert response.status_code == 422
    svc.save_one.assert_not_awaited()


def test_save_missing_place_core_id_returns_422(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    client = _make_app(svc, dispatcher)

    response = client.post(
        "/v1/user/places", json={"recommendation_id": _REC_ID}
    )

    assert response.status_code == 422
    svc.save_one.assert_not_awaited()


def test_save_missing_recommendation_id_returns_422(
    svc: AsyncMock, dispatcher: AsyncMock
) -> None:
    client = _make_app(svc, dispatcher)

    response = client.post("/v1/user/places", json={"place_core_id": "p1"})

    assert response.status_code == 422
    svc.save_one.assert_not_awaited()
