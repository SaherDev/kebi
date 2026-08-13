"""Tests for POST /v1/knowledge/curate — the curator gate + shape (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_knowledge_curation_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.knowledge import router as knowledge_router
from kebi.core.areas.keys import encode_area_id
from kebi.core.knowledge.curation_service import (
    AnchorNotFoundError,
    KnowledgeCurationService,
)
from kebi.core.knowledge.schemas import ResolvedGeo, StructuredClaim, WrittenClaim

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _make_app(service: AsyncMock, *, can_curate: bool) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(knowledge_router, prefix="/v1")
    app.dependency_overrides[get_knowledge_curation_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID, can_curate=can_curate
    )
    return TestClient(app)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=KnowledgeCurationService)
    service.curate = AsyncMock(
        return_value=[
            WrittenClaim(
                id="claim-abc",
                claim=StructuredClaim(
                    scope="city",
                    entity_name="Dubai",
                    claim="rooftop bars are the scene",
                    tags=["nightlife"],
                    confidence=0.9,
                    geo=ResolvedGeo(country_code="ae", city="Dubai"),
                ),
            )
        ]
    )
    return service


def test_forbidden_without_can_curate(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=False)
    resp = client.post("/v1/knowledge/curate", json={"text": "Dubai is great"})
    assert resp.status_code == 403
    svc.curate.assert_not_called()


def test_curate_stores_and_projects_claims(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post("/v1/knowledge/curate", json={"text": "Dubai nightlife..."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["claims_written"] == 1
    assert body["claims"][0]["id"] == "claim-abc"
    assert body["claims"][0]["scope"] == "city"
    assert body["claims"][0]["entity_name"] == "Dubai"
    assert body["claims"][0]["tags"] == ["nightlife"]


def test_empty_text_rejected(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post("/v1/knowledge/curate", json={"text": ""})
    assert resp.status_code == 422  # min_length=1


def test_place_anchor_passes_place_id_through(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "cash only", "anchor": {"place_id": "p1"}},
    )
    assert resp.status_code == 200
    kwargs = svc.curate.await_args.kwargs
    assert kwargs["anchor_place_id"] == "p1"
    assert kwargs["anchor_geo_key"] is None


def test_area_anchor_decoded_to_geo_key(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    token = encode_area_id("id/bali/canggu")
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "great for coffee", "anchor": {"area_id": token}},
    )
    assert resp.status_code == 200
    kwargs = svc.curate.await_args.kwargs
    assert kwargs["anchor_place_id"] is None
    assert kwargs["anchor_geo_key"] == "id/bali/canggu"


def test_anchor_with_both_refs_rejected(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "x", "anchor": {"place_id": "p1", "area_id": "t"}},
    )
    assert resp.status_code == 422
    svc.curate.assert_not_called()


def test_anchor_with_neither_ref_rejected(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post("/v1/knowledge/curate", json={"text": "x", "anchor": {}})
    assert resp.status_code == 422
    svc.curate.assert_not_called()


def test_undecodable_area_token_is_404_before_llm(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "x", "anchor": {"area_id": "!!not-a-token!!"}},
    )
    assert resp.status_code == 404
    svc.curate.assert_not_called()


def test_unknown_place_anchor_is_404(svc: AsyncMock) -> None:
    svc.curate = AsyncMock(side_effect=AnchorNotFoundError("ghost"))
    client = _make_app(svc, can_curate=True)
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "x", "anchor": {"place_id": "ghost"}},
    )
    assert resp.status_code == 404


def test_location_hint_no_longer_accepted(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.post(
        "/v1/knowledge/curate",
        json={"text": "x", "location_hint": {"country_alpha2": "ae"}},
    )
    assert resp.status_code == 422  # extra="forbid": the field is gone
