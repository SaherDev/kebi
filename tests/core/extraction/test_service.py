"""Tests for ExtractionService (spec 030 Phase 3 — inline v2 persistence)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.api.schemas.extract_place import ExtractPlaceResponse
from kebi.core.extraction.extraction_pipeline import TooManyCandidatesError
from kebi.core.extraction.service import ExtractionService
from kebi.core.extraction.types import (
    Evidence,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places_v2 import (
    DuplicateUserPlaceError,
    PlaceCategory,
    PlaceCore,
    PlaceSource,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate(
    place_name: str = "Fuji Ramen",
    provider_id: str = "google:ChIJfuji",
    confidence: float = 0.87,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        provider_id=provider_id,
        categories=[PlaceCategory.restaurant],
        tags=[],
        confidence=confidence,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )


def _persisted_core(
    place_id: str = "place-uuid-1",
    provider_id: str = "google:ChIJfuji",
    place_name: str = "Fuji Ramen",
) -> PlaceCore:
    return PlaceCore(
        id=place_id,
        provider_id=provider_id,
        place_name=place_name,
        categories=[PlaceCategory.restaurant],
    )


def _build_service(
    pipeline_result: list[ValidatedCandidate] | None = None,
    pipeline_exc: Exception | None = None,
    upsert_result: list[PlaceCore] | None = None,
    duplicate_place_ids: list[str] | None = None,
) -> tuple[ExtractionService, MagicMock]:
    """Build ExtractionService with mocked collaborators.

    `duplicate_place_ids`: if non-empty, `save_places` raises
    `DuplicateUserPlaceError(conflicts=duplicate_place_ids)` on the
    first call; the second retry (with filtered cores) succeeds.
    """
    pipeline = MagicMock()
    pipeline.run = AsyncMock(
        side_effect=pipeline_exc if pipeline_exc else None,
        return_value=pipeline_result if pipeline_result is not None else [],
    )
    if pipeline_exc:
        pipeline.run.side_effect = pipeline_exc

    upsert_service = MagicMock()
    upsert_service.upsert_and_embed = AsyncMock(
        return_value=upsert_result if upsert_result is not None else []
    )

    user_places_service = MagicMock()
    if duplicate_place_ids:
        # First call raises with conflicts; retry call returns [].
        user_places_service.save_places = AsyncMock(
            side_effect=[
                DuplicateUserPlaceError(conflicts=list(duplicate_place_ids)),
                [],
            ]
        )
    else:
        user_places_service.save_places = AsyncMock(return_value=[])

    status_repo = MagicMock()
    status_repo.write = AsyncMock()

    event_dispatcher = MagicMock()
    event_dispatcher.dispatch = AsyncMock()

    service = ExtractionService(
        pipeline=pipeline,
        upsert_service=upsert_service,
        user_places_service=user_places_service,
        status_repo=status_repo,
        event_dispatcher=event_dispatcher,
    )

    container = MagicMock()
    container.pipeline = pipeline
    container.upsert = upsert_service
    container.user_places = user_places_service
    container.status_repo = status_repo
    container.event_dispatcher = event_dispatcher
    return service, container


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_raw_input_raises() -> None:
    service, _ = _build_service()
    with pytest.raises(ValueError, match="cannot be empty"):
        await service.run(raw_input="   ", user_id="u1")


@pytest.mark.asyncio
async def test_unsupported_url_returns_failed_envelope() -> None:
    service, c = _build_service()
    resp = await service.run(
        raw_input="https://example.com/blog/post", user_id="u1"
    )
    assert resp.status == "failed"
    assert resp.failure_reason == "unsupported_url"
    assert resp.results == []
    # Status envelope written even on failure path.
    c.status_repo.write.assert_called_once()
    # Pipeline never invoked.
    c.pipeline.run.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_returns_empty_failed_no_candidates() -> None:
    service, c = _build_service(pipeline_result=[])
    resp = await service.run(raw_input="something", user_id="u1")
    assert resp.status == "failed"
    assert resp.failure_reason == "no_candidates"
    c.upsert.upsert_and_embed.assert_not_called()


@pytest.mark.asyncio
async def test_too_many_candidates_returns_failed_envelope() -> None:
    service, c = _build_service(
        pipeline_exc=TooManyCandidatesError(found=30, limit=25)
    )
    resp = await service.run(raw_input="something", user_id="u1")
    assert resp.status == "failed"
    assert resp.failure_reason == "candidate_limit_exceeded"
    assert "30" in (resp.failure_message or "")
    c.upsert.upsert_and_embed.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_exception_returns_pipeline_error() -> None:
    service, c = _build_service(pipeline_exc=RuntimeError("boom"))
    resp = await service.run(raw_input="something", user_id="u1")
    assert resp.status == "failed"
    assert resp.failure_reason == "pipeline_error"
    assert "RuntimeError" in (resp.failure_message or "")


@pytest.mark.asyncio
async def test_successful_save_emits_completed_with_saved_status() -> None:
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate],
        upsert_result=[persisted],
    )
    resp = await service.run(raw_input="Fuji Ramen", user_id="u1")
    assert resp.status == "completed"
    assert len(resp.results) == 1
    assert resp.results[0].status == "saved"
    assert resp.results[0].place.place_name == "Fuji Ramen"
    c.upsert.upsert_and_embed.assert_awaited_once()
    c.user_places.save_places.assert_awaited_once()
    c.event_dispatcher.dispatch.assert_awaited_once()
    event = c.event_dispatcher.dispatch.await_args.args[0]
    assert event.place_ids == ["place-uuid-1"]
    assert event.user_id == "u1"


@pytest.mark.asyncio
async def test_duplicate_user_place_emits_duplicate_status() -> None:
    """save_places raises DuplicateUserPlaceError; the only candidate
    is in the conflicts list → it surfaces as `status="duplicate"` and
    no retry is needed."""
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate],
        upsert_result=[persisted],
        duplicate_place_ids=["place-uuid-1"],
    )
    resp = await service.run(raw_input="Fuji Ramen", user_id="u1")
    assert resp.status == "completed"
    assert resp.results[0].status == "duplicate"
    # First call raises with conflicts; no retry call because every
    # candidate was a conflict.
    assert c.user_places.save_places.await_count == 1
    # No PlaceSaved dispatch for a duplicate.
    c.event_dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_saved_and_duplicate() -> None:
    c1 = _candidate(place_name="Fuji", provider_id="google:fuji")
    c2 = _candidate(place_name="Joe Pizza", provider_id="google:joe")
    p1 = _persisted_core(place_id="p1", provider_id="google:fuji", place_name="Fuji")
    p2 = _persisted_core(
        place_id="p2", provider_id="google:joe", place_name="Joe Pizza"
    )
    service, c = _build_service(
        pipeline_result=[c1, c2],
        upsert_result=[p1, p2],
        duplicate_place_ids=["p2"],  # joe already linked
    )
    resp = await service.run(raw_input="some text", user_id="u1")
    assert resp.status == "completed"
    statuses = [r.status for r in resp.results]
    assert statuses == ["saved", "duplicate"]
    # Two calls: first raises, second retries with only p1.
    assert c.user_places.save_places.await_count == 2
    retry_args = c.user_places.save_places.await_args.kwargs
    assert [p.id for p in retry_args["places"]] == ["p1"]


@pytest.mark.asyncio
async def test_status_repo_written_for_every_terminal_envelope() -> None:
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate], upsert_result=[persisted]
    )
    resp = await service.run(raw_input="Fuji Ramen", user_id="u1")
    c.status_repo.write.assert_awaited_once()
    _key, payload = c.status_repo.write.await_args.args
    assert payload["status"] == "completed"
    # Envelope keys check
    assert isinstance(resp, ExtractPlaceResponse)


@pytest.mark.asyncio
async def test_save_places_called_with_v2_source() -> None:
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate], upsert_result=[persisted]
    )
    await service.run(
        raw_input="https://www.tiktok.com/@x/video/1", user_id="u1"
    )
    c.user_places.save_places.assert_awaited_once()
    kwargs = c.user_places.save_places.await_args.kwargs
    assert kwargs["source"] == PlaceSource.tiktok
    assert kwargs["source_url"] == "https://www.tiktok.com/@x/video/1"
