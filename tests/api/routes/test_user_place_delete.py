"""Tests for DELETE /v1/user/places/{user_place_id}.

Ownership is enforced in the repo (the delete is scoped on user_id); at the
route level the contract is: 204 whether or not a row was removed, identity
threaded from the gateway, never from the path or body.
"""

from __future__ import annotations

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
from kebi.core.places import UserPlacesService

_TEST_USER_ID = "user_test_dummy_123456789012345"


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
    service.delete_place = AsyncMock(return_value=True)
    return service


def test_delete_returns_204_with_empty_body(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.delete("/v1/user/places/up-1")

    assert response.status_code == 204
    assert response.content == b""
    svc.delete_place.assert_awaited_once_with("up-1", _TEST_USER_ID)


def test_delete_threads_gateway_identity_not_path(svc: AsyncMock) -> None:
    """user_id comes only from the verified gateway identity — a caller
    cannot target another user by anything in the path."""
    client = _make_app(svc)

    client.delete("/v1/user/places/some-other-users-row")

    _, kwargs = svc.delete_place.await_args
    args = svc.delete_place.await_args.args
    assert args == ("some-other-users-row", _TEST_USER_ID)
    assert kwargs == {}


def test_delete_absent_or_not_owned_returns_404(svc: AsyncMock) -> None:
    """Service returns False (nothing matched). The route answers 404 for
    both 'does not exist' and 'belongs to another user' — indistinguishable,
    so the response leaks nothing about other users' saves."""
    svc.delete_place = AsyncMock(return_value=False)
    client = _make_app(svc)

    response = client.delete("/v1/user/places/up-missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "saved_place_not_found"


def test_delete_not_yours_indistinguishable_from_absent(svc: AsyncMock) -> None:
    """A real save owned by someone else and a nonexistent id must produce
    the identical 404 — no existence leak across users."""
    svc.delete_place = AsyncMock(return_value=False)
    client = _make_app(svc)

    not_yours = client.delete("/v1/user/places/up-owned-by-someone-else")
    absent = client.delete("/v1/user/places/up-never-existed")

    assert not_yours.status_code == absent.status_code == 404
    assert not_yours.json() == absent.json()


def test_delete_second_call_404_after_success(svc: AsyncMock) -> None:
    """First delete removes the row (204); a repeat finds nothing (404)."""
    svc.delete_place = AsyncMock(side_effect=[True, False])
    client = _make_app(svc)

    first = client.delete("/v1/user/places/up-1")
    second = client.delete("/v1/user/places/up-1")

    assert first.status_code == 204
    assert second.status_code == 404
    assert svc.delete_place.await_count == 2


def test_delete_service_exception_returns_500(svc: AsyncMock) -> None:
    svc.delete_place.side_effect = RuntimeError("boom")
    client = _make_app(svc)

    response = client.delete("/v1/user/places/up-1")

    assert response.status_code == 500
