"""Tests for GET /v1/knowledge/claims + DELETE /v1/knowledge/claims/{id}."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_curator_claims_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.knowledge import router as knowledge_router
from kebi.core.areas.keys import decode_area_id
from kebi.core.knowledge.curation_service import CuratorClaimsService
from kebi.core.knowledge.schemas import KnowledgeClaim

_TEST_USER_ID = "user_test_dummy_123456789012345"
_CREATED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _claim(claim_id: str, entity_type: str, entity_key: str) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=claim_id,
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_key=entity_key,
        entity_name="Somewhere",
        claim="a fact",
        tags=["nightlife"],
        source_type="curated_expert",
        source_ref=f"curator:{_TEST_USER_ID}",
        confidence=0.9,
        created_at=_CREATED,
    )


def _make_app(service: AsyncMock, *, can_curate: bool) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(knowledge_router, prefix="/v1")
    app.dependency_overrides[get_curator_claims_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID, can_curate=can_curate
    )
    return TestClient(app)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=CuratorClaimsService)
    service.list_mine = AsyncMock(
        return_value=(
            [
                _claim("c-place", "place", "place:p1"),
                _claim("c-area", "neighborhood", "id/bali/canggu"),
            ],
            "next-token",
        )
    )
    service.retract = AsyncMock(return_value=True)
    return service


def test_list_forbidden_without_can_curate(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=False)
    assert client.get("/v1/knowledge/claims").status_code == 403
    svc.list_mine.assert_not_called()


def test_delete_forbidden_without_can_curate(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=False)
    assert client.delete("/v1/knowledge/claims/c1").status_code == 403
    svc.retract.assert_not_called()


def test_list_projects_claims_with_anchors(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.get("/v1/knowledge/claims")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] == "next-token"

    place = body["claims"][0]
    assert place["id"] == "c-place"
    assert place["scope"] == "place"
    assert place["anchor"] == {
        "type": "place",
        "place_id": "p1",
        "area_id": None,
        "name": "Somewhere",
    }

    area = body["claims"][1]
    assert area["scope"] == "neighborhood"
    assert area["anchor"]["type"] == "area"
    assert area["anchor"]["place_id"] is None
    # The token is the same encoding kebi://area/{id} links carry.
    assert decode_area_id(area["anchor"]["area_id"]) == "id/bali/canggu"

    # ADR-105: provenance and trust internals never leave the service.
    assert "confidence" not in place
    assert "source_ref" not in place
    assert "source_type" not in place


def test_list_scoped_to_caller(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    client.get("/v1/knowledge/claims?limit=5&cursor=abc")
    svc.list_mine.assert_awaited_once_with(_TEST_USER_ID, 5, "abc")


def test_delete_own_claim_204(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.delete("/v1/knowledge/claims/c-place")
    assert resp.status_code == 204
    svc.retract.assert_awaited_once_with(_TEST_USER_ID, "c-place")


def test_delete_missing_or_foreign_claim_404(svc: AsyncMock) -> None:
    svc.retract = AsyncMock(return_value=False)
    client = _make_app(svc, can_curate=True)
    assert client.delete("/v1/knowledge/claims/ghost").status_code == 404
