"""Tests for GET /v1/home (greeting + suggestion chips)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_home_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.home import router as home_router
from kebi.core.home import HomeService
from kebi.core.home.schemas import HomeChip, HomeSuggestion

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _make_app(service: AsyncMock) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(home_router, prefix="/v1")
    app.dependency_overrides[get_home_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID
    )
    return TestClient(app)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=HomeService)
    service.generate = AsyncMock(
        return_value=HomeSuggestion(
            greeting="it's late, drunk food?",
            chips=[HomeChip(text="ramen, no line"), HomeChip(text="surprise me")],
        )
    )
    return service


def test_returns_greeting_and_chips(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/home")

    assert resp.status_code == 200
    body = resp.json()
    assert body["greeting"] == "it's late, drunk food?"
    assert [c["text"] for c in body["chips"]] == ["ramen, no line", "surprise me"]


def test_context_passed_through(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get(
        "/v1/home",
        params={
            "lat": "13.7",
            "lng": "100.5",
            "city": "shimokitazawa",
            "local_time": "2026-06-28T21:41:00",
            "weather": "clear",
        },
    )

    assert resp.status_code == 200
    user_id, context = svc.generate.await_args.args
    assert user_id == _TEST_USER_ID
    assert context.city == "shimokitazawa"
    assert context.weather == "clear"
    assert context.lat == 13.7


def test_no_context_still_ok(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/home")

    assert resp.status_code == 200
    _, context = svc.generate.await_args.args
    assert context.lat is None and context.city is None


def test_unknown_param_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/home", params={"bogus": "1"})

    assert resp.status_code == 422


def test_out_of_range_lat_rejected_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    resp = client.get("/v1/home", params={"lat": "999"})

    assert resp.status_code == 422
