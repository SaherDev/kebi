"""Tests for PATCH /v1/user/places/{user_place_id}.

The route's contract: partial body (omitted ≠ null), identity from the
gateway never the path, the updated user-state DTO on success (never the raw
domain model — ADR-105), and a 404 that can't distinguish absent from
not-yours.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_user_places_service,
    require_gateway_identity,
)
from kebi.api.routes.user import router as user_router
from kebi.core.places import PlaceSource, UserPlace, UserPlacesService

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _updated(**overrides: object) -> UserPlace:
    base = {
        "user_place_id": "up-1",
        "user_id": _TEST_USER_ID,
        "place_id": "p1",
        "source": PlaceSource.manual,
        "saved_at": datetime(2026, 6, 9, tzinfo=UTC),
    }
    base.update(overrides)
    return UserPlace(**base)  # type: ignore[arg-type]


def _make_app(service: UserPlacesService) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_places_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=UserPlacesService)
    service.update_status = AsyncMock(return_value=_updated(visited=True))
    return service


def test_patch_returns_updated_user_state_dto(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.patch("/v1/user/places/up-1", json={"visited": True})

    assert response.status_code == 200
    body = response.json()
    assert body["visited"] is True
    assert body["user_place_id"] == "up-1"


def test_patch_response_omits_user_id(svc: AsyncMock) -> None:
    """ADR-105 — the caller's identity is never echoed back in the payload."""
    client = _make_app(svc)

    response = client.patch("/v1/user/places/up-1", json={"approved": True})

    assert response.status_code == 200
    assert "user_id" not in response.json()


def test_patch_threads_gateway_identity_and_only_set_fields(svc: AsyncMock) -> None:
    client = _make_app(svc)

    client.patch("/v1/user/places/up-1", json={"liked": True})

    user_place_id, user_id, update = svc.update_status.await_args.args
    assert user_place_id == "up-1"
    assert user_id == _TEST_USER_ID
    # Only the field the client sent reaches the domain update.
    assert update.model_dump(exclude_unset=True) == {"liked": True}


def test_patch_explicit_null_clears_field(svc: AsyncMock) -> None:
    """An explicit null is a real change (clear the note), not 'omitted'."""
    client = _make_app(svc)

    client.patch("/v1/user/places/up-1", json={"note": None})

    update = svc.update_status.await_args.args[2]
    assert update.model_dump(exclude_unset=True) == {"note": None}


def test_patch_empty_body_returns_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.patch("/v1/user/places/up-1", json={})

    assert response.status_code == 422
    svc.update_status.assert_not_awaited()


def test_patch_unknown_field_returns_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.patch("/v1/user/places/up-1", json={"bogus": True})

    assert response.status_code == 422
    svc.update_status.assert_not_awaited()


def test_patch_absent_or_not_owned_returns_404(svc: AsyncMock) -> None:
    """Service returns None (nothing matched). 404 for both 'does not exist'
    and 'belongs to another user' — indistinguishable, no existence leak."""
    svc.update_status = AsyncMock(return_value=None)
    client = _make_app(svc)

    response = client.patch("/v1/user/places/up-missing", json={"visited": True})

    assert response.status_code == 404
    assert response.json()["detail"] == "saved_place_not_found"
