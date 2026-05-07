"""Route tests for /v1/extract (POST) and /v1/extraction/{request_id} (GET)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kebi.api.deps import get_extraction_service, get_status_repo
from kebi.api.main import app
from kebi.api.schemas.extract_place import ExtractPlaceResponse
from kebi.core.extraction.service import ExtractionService
from kebi.core.extraction.status_repository import ExtractionStatusRepository


@pytest.fixture
def mock_status_repo() -> AsyncMock:
    return AsyncMock(spec=ExtractionStatusRepository)


@pytest.fixture
def client(mock_status_repo: AsyncMock) -> TestClient:
    app.dependency_overrides[get_status_repo] = lambda: mock_status_repo
    yield TestClient(app)
    app.dependency_overrides.pop(get_status_repo, None)


def _sample_place() -> dict:
    return {
        "place_id": "pl_test_01",
        "place_name": "Nara Eatery",
        "place_type": "food_and_drink",
        "subcategory": "restaurant",
        "tags": ["ramen"],
        "attributes": {
            "cuisine": "japanese",
            "price_hint": None,
            "ambiance": None,
            "dietary": [],
            "good_for": [],
            "location_context": None,
        },
        "source_url": "https://tiktok.com/@x/video/123",
        "source": "tiktok",
        "provider_id": "google:ChIJTest",
        "created_at": "2026-04-21T10:00:00+00:00",
        "lat": None,
        "lng": None,
        "address": None,
        "geo_fresh": False,
        "hours": None,
        "rating": None,
        "phone": None,
        "photo_url": None,
        "popularity": None,
        "enriched": False,
    }


class TestPollingRouteCompleted:
    def test_returns_completed_envelope(
        self, client: TestClient, mock_status_repo: AsyncMock
    ) -> None:
        mock_status_repo.read.return_value = {
            "status": "completed",
            "results": [
                {
                    "place": _sample_place(),
                    "confidence": 0.87,
                    "status": "saved",
                }
            ],
            "raw_input": "https://tiktok.com/@x/video/123",
            "request_id": "req_abc",
        }
        resp = client.get("/v1/extraction/req_abc")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "saved"
        assert body["results"][0]["place"]["place_id"] == "pl_test_01"
        assert body["results"][0]["confidence"] == 0.87
        assert body["raw_input"] == "https://tiktok.com/@x/video/123"


class TestPollingRouteFailed:
    def test_returns_failed_envelope_with_empty_results(
        self, client: TestClient, mock_status_repo: AsyncMock
    ) -> None:
        mock_status_repo.read.return_value = {
            "status": "failed",
            "results": [],
            "raw_input": "gibberish with no place",
            "request_id": "req_fail",
            "failure_reason": "no_candidates",
            "failure_message": "Nothing extractable",
        }
        resp = client.get("/v1/extraction/req_fail")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["results"] == []
        assert body["raw_input"] == "gibberish with no place"


class TestPollingRouteMissing:
    def test_missing_key_returns_404(
        self, client: TestClient, mock_status_repo: AsyncMock
    ) -> None:
        """Legacy `extraction:v1:*` keys return 404, same code path as TTL
        expiry (ADR-063 clarification).
        """
        mock_status_repo.read.return_value = None
        resp = client.get("/v1/extraction/req_missing")
        assert resp.status_code == 404


class TestExtractRoute:
    """`POST /v1/extract` invokes the extraction service directly,
    bypassing the agent."""

    def test_post_extract_returns_envelope(self) -> None:
        mock_service = AsyncMock(spec=ExtractionService)
        mock_service.run.return_value = ExtractPlaceResponse(
            status="completed",
            results=[
                {
                    "place": _sample_place(),
                    "confidence": 0.87,
                    "status": "saved",
                    "evidence": [
                        {
                            "producer": "llm_ner",
                            "medium": "caption",
                            "snippet": "Loved Nara Eatery",
                            "metadata": {},
                        }
                    ],
                }
            ],
            raw_input="https://tiktok.com/@x/video/123",
            request_id="req_abc",
        )
        app.dependency_overrides[get_extraction_service] = lambda: mock_service
        try:
            client = TestClient(app)
            resp = client.post(
                "/v1/extract",
                json={
                    "user_id": "u1",
                    "raw_input": "https://tiktok.com/@x/video/123",
                },
            )
        finally:
            app.dependency_overrides.pop(get_extraction_service, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["results"][0]["evidence"][0]["producer"] == "llm_ner"
        mock_service.run.assert_awaited_once_with(
            raw_input="https://tiktok.com/@x/video/123", user_id="u1"
        )

    def test_post_extract_returns_failed_envelope(self) -> None:
        mock_service = AsyncMock(spec=ExtractionService)
        mock_service.run.return_value = ExtractPlaceResponse(
            status="failed",
            results=[],
            raw_input="gibberish",
            request_id="req_fail",
            failure_reason="no_candidates",
            failure_message="No venue found in 'gibberish'",
        )
        app.dependency_overrides[get_extraction_service] = lambda: mock_service
        try:
            client = TestClient(app)
            resp = client.post(
                "/v1/extract",
                json={"user_id": "u1", "raw_input": "gibberish"},
            )
        finally:
            app.dependency_overrides.pop(get_extraction_service, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["results"] == []


class TestPollingStatusRepoWritesV2Prefix:
    """Verify the status repo uses the bumped `extraction:v2:` prefix."""

    async def test_write_uses_v2_prefix(self) -> None:
        from kebi.providers.cache import CacheBackend

        mock_cache = AsyncMock(spec=CacheBackend)
        repo = ExtractionStatusRepository(cache=mock_cache)
        await repo.write("req_123", {"status": "failed", "results": []})
        mock_cache.set.assert_awaited_once()
        written_key = mock_cache.set.call_args.args[0]
        assert written_key == "extraction:v2:req_123"

    async def test_read_uses_v2_prefix(self) -> None:
        from kebi.providers.cache import CacheBackend

        mock_cache = AsyncMock(spec=CacheBackend)
        mock_cache.get.return_value = None
        repo = ExtractionStatusRepository(cache=mock_cache)
        await repo.read("req_456")
        mock_cache.get.assert_awaited_once_with("extraction:v2:req_456")
