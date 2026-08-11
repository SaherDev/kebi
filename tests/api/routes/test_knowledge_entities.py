"""Tests for GET /v1/knowledge/entities — anchor-chip typeahead route."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kebi.api.deps import (
    GatewayIdentity,
    get_entity_search_service,
    require_gateway_identity,
)
from kebi.api.errors import register_error_handlers
from kebi.api.routes.knowledge import router as knowledge_router
from kebi.core.areas.keys import decode_area_id
from kebi.core.knowledge.entity_search_service import (
    AreaHit,
    EntitySearchResults,
    EntitySearchService,
)
from kebi.core.places.models import LocationContext, PlaceCore

_TEST_USER_ID = "user_test_dummy_123456789012345"


def _make_app(service: AsyncMock, *, can_curate: bool) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(knowledge_router, prefix="/v1")
    app.dependency_overrides[get_entity_search_service] = lambda: service
    app.dependency_overrides[require_gateway_identity] = lambda: GatewayIdentity(
        user_id=_TEST_USER_ID, can_curate=can_curate
    )
    return TestClient(app)


@pytest.fixture
def svc() -> AsyncMock:
    service = AsyncMock(spec=EntitySearchService)
    service.search = AsyncMock(
        return_value=EntitySearchResults(
            areas=[AreaHit(geo_key="id/bali/canggu", name="Canggu", level="area")],
            places=[
                PlaceCore(
                    id="p1",
                    place_name="Canggu Coffee Lab",
                    icon="☕",
                    location=LocationContext(
                        neighborhood="Canggu", city="Bali", country="Indonesia"
                    ),
                )
            ],
        )
    )
    return service


def test_forbidden_without_can_curate(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=False)
    assert client.get("/v1/knowledge/entities?q=cang").status_code == 403
    svc.search.assert_not_called()


def test_typed_results_areas_first(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    resp = client.get("/v1/knowledge/entities?q=cang")
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["type"] for r in results] == ["area", "place"]

    area = results[0]
    assert area["name"] == "Canggu"
    assert area["place_id"] is None
    # The token is the same encoding area links and GET /v1/areas/{id} take.
    assert decode_area_id(area["area_id"]) == "id/bali/canggu"
    assert area["context"] == "Bali, ID"

    place = results[1]
    assert place["place_id"] == "p1"
    assert place["area_id"] is None
    assert place["icon"] == "☕"
    assert place["context"] == "Canggu, Bali"


def test_query_length_bounds(svc: AsyncMock) -> None:
    client = _make_app(svc, can_curate=True)
    assert client.get("/v1/knowledge/entities?q=c").status_code == 422
    assert client.get("/v1/knowledge/entities").status_code == 422


def test_empty_results_are_200(svc: AsyncMock) -> None:
    svc.search = AsyncMock(return_value=EntitySearchResults(areas=[], places=[]))
    client = _make_app(svc, can_curate=True)
    resp = client.get("/v1/knowledge/entities?q=zzzz")
    assert resp.status_code == 200
    assert resp.json() == {"results": []}
