"""Tests for POST /v1/signal recommendation accept/reject (ADR-060)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import get_signal_service
from kebi.api.routes.signal import router as signal_router
from kebi.core.signal.service import SignalService


def _make_app(service: SignalService) -> TestClient:
    app = FastAPI()
    app.include_router(signal_router, prefix="/v1")
    app.dependency_overrides[get_signal_service] = lambda: service
    return TestClient(app)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=SignalService)
    service.handle_signal = AsyncMock()
    return service


def test_unknown_signal_type_returns_422(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.post(
        "/v1/signal",
        json={
            "signal_type": "no_such_type",
            "user_id": "user_abc",
            "recommendation_id": "rec_1",
            "place_core_id": "pl_1",
        },
    )

    assert response.status_code == 422


def test_recommendation_accepted_still_routes_correctly(svc: AsyncMock) -> None:
    client = _make_app(svc)

    response = client.post(
        "/v1/signal",
        json={
            "signal_type": "recommendation_accepted",
            "user_id": "user_abc",
            "recommendation_id": "rec_1",
            "place_core_id": "pl_1",
        },
    )

    assert response.status_code == 202
    svc.handle_signal.assert_awaited_once()
    call_kwargs = svc.handle_signal.await_args.kwargs
    assert call_kwargs["signal_type"] == "recommendation_accepted"
    assert call_kwargs["recommendation_id"] == "rec_1"
