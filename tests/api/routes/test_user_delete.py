"""Tests for DELETE /v1/user/{user_id}/data."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import get_user_data_deletion_service
from kebi.api.routes.user import router as user_router
from kebi.core.user.service import DataScope, UserDataDeletionService


def _make_app(service: UserDataDeletionService) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_data_deletion_service] = lambda: service
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=UserDataDeletionService)
    service.delete_user_data = AsyncMock(return_value=None)
    return service


def test_delete_user_data_returns_204_with_empty_body(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.delete("/v1/user/user_abc/data")

    assert response.status_code == 204
    assert response.content == b""
    svc.delete_user_data.assert_awaited_once_with("user_abc", scopes=None)


def test_delete_user_data_idempotent_second_call_also_204(svc: AsyncMock) -> None:
    client = _make_app(svc)

    first = client.delete("/v1/user/user_abc/data")
    second = client.delete("/v1/user/user_abc/data")

    assert first.status_code == 204
    assert second.status_code == 204
    assert svc.delete_user_data.await_count == 2


def test_delete_user_data_scope_chat_history_passes_scope_set(
    svc: AsyncMock,
) -> None:
    """`?scope=chat_history` should narrow the service call to only the
    chat_history scope, leaving SQL data alone."""
    client = _make_app(svc)

    response = client.delete("/v1/user/user_abc/data?scope=chat_history")

    assert response.status_code == 204
    svc.delete_user_data.assert_awaited_once_with(
        "user_abc", scopes={DataScope.chat_history}
    )


def test_delete_user_data_scope_all_explicit_passes_all_scope(
    svc: AsyncMock,
) -> None:
    client = _make_app(svc)

    response = client.delete("/v1/user/user_abc/data?scope=all")

    assert response.status_code == 204
    svc.delete_user_data.assert_awaited_once_with(
        "user_abc", scopes={DataScope.all}
    )


def test_delete_user_data_unknown_scope_returns_422(svc: AsyncMock) -> None:
    """FastAPI's native enum validation rejects unknown values with 422."""
    client = _make_app(svc)

    response = client.delete("/v1/user/user_abc/data?scope=bogus")

    assert response.status_code == 422
    svc.delete_user_data.assert_not_awaited()


def test_delete_user_data_repeated_scope_param_collected_into_set(
    svc: AsyncMock,
) -> None:
    """FastAPI parses `?scope=a&scope=b` into a list; the route folds it
    into a set before handing off to the service."""
    client = _make_app(svc)

    response = client.delete(
        "/v1/user/user_abc/data?scope=chat_history&scope=all"
    )

    assert response.status_code == 204
    svc.delete_user_data.assert_awaited_once_with(
        "user_abc", scopes={DataScope.chat_history, DataScope.all}
    )


def test_delete_user_data_service_exception_returns_500(svc: AsyncMock) -> None:
    """An unhandled service exception should surface as 500. We don't
    register the central error handler in this test app, so FastAPI's
    default 500 handler runs — same status code as production."""
    svc.delete_user_data.side_effect = RuntimeError("boom")
    client = _make_app(svc)

    response = client.delete("/v1/user/user_abc/data")

    assert response.status_code == 500


def test_delete_user_data_url_safe_user_ids(svc: AsyncMock) -> None:
    """Path-encoded characters should round-trip cleanly."""
    client = _make_app(svc)

    response = client.delete("/v1/user/user-with-dash_and_underscore.123/data")

    assert response.status_code == 204
    svc.delete_user_data.assert_awaited_once_with(
        "user-with-dash_and_underscore.123", scopes=None
    )
