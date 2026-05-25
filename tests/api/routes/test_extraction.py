"""Route tests for /v1/extract (POST)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from kebi.api.deps import get_extraction_service
from kebi.api.main import app
from kebi.api.schemas.extract_place import ExtractPlaceResponse
from kebi.core.extraction.service import ExtractionService


def _sample_place() -> dict:
    return {
        "id": "pl_test_01",
        "provider_id": "google:ChIJTest",
        "place_name": "Nara Eatery",
        "place_name_aliases": [],
        "categories": ["restaurant"],
        "tags": [
            {"type": "cuisine", "value": "Japanese", "source": "llm"},
        ],
        "location": None,
        "created_at": "2026-04-21T10:00:00+00:00",
        "refreshed_at": None,
    }


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
        # Evidence no longer rides the response; it ships to the bucket
        # ledger instead. The product repo never sees it.
        assert "evidence" not in body["results"][0]
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
