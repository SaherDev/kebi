"""Tests for GET /v1/user/intents (the "what you wanted" recall list)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_user_intent_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.user import router as user_router
from kebi.core.user.intent_models import IntentRecord
from kebi.core.user.intent_service import UserIntentService

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _make_app(service: AsyncMock) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(user_router, prefix="/v1")
    app.dependency_overrides[get_user_intent_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    return TestClient(app)


def _record(id_: str, text: str) -> IntentRecord:
    return IntentRecord(id=id_, text=text, created_at=datetime(2026, 6, 28, tzinfo=UTC))


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=UserIntentService)
    service.list_intents = AsyncMock(
        return_value=([_record("i1", "coffee, quiet")], "next-tok")
    )
    return service


def test_returns_intents_and_cursor(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/intents")

    assert resp.status_code == 200
    body = resp.json()
    assert [i["text"] for i in body["intents"]] == ["coffee, quiet"]
    assert body["next_cursor"] == "next-tok"

    # user_id from gateway identity; default limit 20, no cursor.
    args = svc.list_intents.await_args.args
    assert args[0] == _TEST_USER_ID
    assert args[1] == 20
    assert args[2] is None


def test_user_id_not_exposed(svc: AsyncMock) -> None:
    client = _make_app(svc)

    body = client.get("/v1/user/intents").json()

    item = body["intents"][0]
    assert "user_id" not in item
    assert set(item.keys()) == {"id", "text", "created_at"}


def test_limit_and_cursor_passed_through(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/intents", params={"limit": "5", "cursor": "abc"})

    assert resp.status_code == 200
    args = svc.list_intents.await_args.args
    assert args[1] == 5
    assert args[2] == "abc"


def test_empty_history(svc: AsyncMock) -> None:
    svc.list_intents = AsyncMock(return_value=([], None))
    client = _make_app(svc)

    resp = client.get("/v1/user/intents")

    assert resp.status_code == 200
    assert resp.json() == {"intents": [], "next_cursor": None}


def test_limit_over_cap_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/intents", params={"limit": "1000"})

    assert resp.status_code == 422


def test_unknown_param_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/user/intents", params={"bogus": "1"})

    assert resp.status_code == 422


def test_malformed_cursor_returns_400(svc: AsyncMock) -> None:
    svc.list_intents = AsyncMock(side_effect=ValueError("invalid intent cursor: '@@'"))
    client = _make_app(svc)

    resp = client.get("/v1/user/intents", params={"cursor": "@@"})

    assert resp.status_code == 400
