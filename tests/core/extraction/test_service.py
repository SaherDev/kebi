"""Tests for ExtractionService (spec 030 Phase 3 — inline v2 persistence)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.api.schemas.extract_place import (
    EvidenceDTO,
    ExtractPlaceItem,
    ExtractPlaceResponse,
)
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
    cache_hit_items: list[ExtractPlaceItem] | None = None,
    save_places_first_call_exc: Exception | None = None,
) -> tuple[ExtractionService, MagicMock]:
    """Build ExtractionService with mocked collaborators.

    `duplicate_place_ids`: if non-empty, `save_places` raises
    `DuplicateUserPlaceError(conflicts=duplicate_place_ids)` on the
    first call; the second retry (with filtered cores) succeeds.

    `cache_hit_items`: pre-populates the result cache for ADR-074
    cache-hit tests. Default `None` = cache miss; tests of the
    miss-path don't need to think about the cache.

    `save_places_first_call_exc`: forces the *first* `save_places`
    call to raise this exception (used for the FK-violation fallback
    test, where the cache-hit path should evict and fall back to the
    full pipeline run).
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
    if save_places_first_call_exc is not None:
        # First call raises (e.g. FK violation from a stale cache
        # entry); subsequent calls succeed.
        user_places_service.save_places = AsyncMock(
            side_effect=[save_places_first_call_exc, [], [], []]
        )
    elif duplicate_place_ids:
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

    result_cache = MagicMock()
    result_cache.get = AsyncMock(return_value=cache_hit_items)
    result_cache.set = AsyncMock()
    result_cache.delete = AsyncMock()

    service = ExtractionService(
        pipeline=pipeline,
        upsert_service=upsert_service,
        user_places_service=user_places_service,
        status_repo=status_repo,
        event_dispatcher=event_dispatcher,
        result_cache=result_cache,
    )

    container = MagicMock()
    container.pipeline = pipeline
    container.upsert = upsert_service
    container.user_places = user_places_service
    container.status_repo = status_repo
    container.event_dispatcher = event_dispatcher
    container.result_cache = result_cache
    return service, container


def _cached_item(
    place_id: str = "place-uuid-1",
    provider_id: str = "google:ChIJfuji",
    place_name: str = "Fuji Ramen",
    confidence: float = 0.87,
) -> ExtractPlaceItem:
    """Build an ExtractPlaceItem suitable for pre-populating the result cache."""
    return ExtractPlaceItem(
        place=PlaceCore(
            id=place_id,
            provider_id=provider_id,
            place_name=place_name,
            categories=[PlaceCategory.restaurant],
        ),
        confidence=confidence,
        evidence=[EvidenceDTO(producer="llm_ner", medium="caption", snippet="…")],
    )


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
    resp = await service.run(raw_input="https://example.com/blog/post", user_id="u1")
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
    service, c = _build_service(pipeline_exc=TooManyCandidatesError(found=30, limit=25))
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
async def test_successful_save_emits_completed_envelope() -> None:
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate],
        upsert_result=[persisted],
    )
    resp = await service.run(raw_input="Fuji Ramen", user_id="u1")
    assert resp.status == "completed"
    assert len(resp.results) == 1
    assert resp.results[0].place.place_name == "Fuji Ramen"
    c.upsert.upsert_and_embed.assert_awaited_once()
    c.user_places.save_places.assert_awaited_once()
    c.event_dispatcher.dispatch.assert_awaited_once()
    event = c.event_dispatcher.dispatch.await_args.args[0]
    assert event.place_core_ids == ["place-uuid-1"]
    assert event.user_id == "u1"


@pytest.mark.asyncio
async def test_duplicate_user_place_skips_event_dispatch() -> None:
    """save_places raises DuplicateUserPlaceError; the only candidate
    is in the conflicts list → it still appears in the response (every
    picker output is in the user's list), but PlaceSaved does not fire
    because no NEW user_places row was created."""
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        pipeline_result=[candidate],
        upsert_result=[persisted],
        duplicate_place_ids=["place-uuid-1"],
    )
    resp = await service.run(raw_input="Fuji Ramen", user_id="u1")
    assert resp.status == "completed"
    assert len(resp.results) == 1
    assert resp.results[0].place.place_name == "Fuji Ramen"
    # First call raises with conflicts; no retry because every candidate
    # was a conflict.
    assert c.user_places.save_places.await_count == 1
    # No PlaceSaved dispatch for a place that was already linked.
    c.event_dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_save_and_duplicate_dispatches_event_for_new_link_only() -> None:
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
    # Both places appear in the response; status is no longer a per-item field.
    place_ids = [r.place.id for r in resp.results]
    assert place_ids == ["p1", "p2"]
    # Two save calls: first raises, second retries with only p1.
    assert c.user_places.save_places.await_count == 2
    retry_args = c.user_places.save_places.await_args.kwargs
    assert [p.id for p in retry_args["places"]] == ["p1"]
    # PlaceSaved fires for the newly linked p1 only.
    c.event_dispatcher.dispatch.assert_awaited_once()
    event = c.event_dispatcher.dispatch.await_args.args[0]
    assert event.place_core_ids == ["p1"]


@pytest.mark.asyncio
async def test_status_repo_written_for_every_terminal_envelope() -> None:
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(pipeline_result=[candidate], upsert_result=[persisted])
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
    service, c = _build_service(pipeline_result=[candidate], upsert_result=[persisted])
    await service.run(raw_input="https://www.tiktok.com/@x/video/1", user_id="u1")
    c.user_places.save_places.assert_awaited_once()
    kwargs = c.user_places.save_places.await_args.kwargs
    assert kwargs["source"] == PlaceSource.tiktok
    assert kwargs["source_url"] == "https://www.tiktok.com/@x/video/1"


# ---------------------------------------------------------------------------
# ADR-074 — extraction result cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_writes_completed_response_to_cache() -> None:
    """When the pipeline runs and produces a completed response, the
    result is written to the cache for next-user reuse."""
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(pipeline_result=[candidate], upsert_result=[persisted])
    resp = await service.run(
        raw_input="https://www.tiktok.com/@x/video/1?web_id=abc", user_id="u1"
    )
    # Pipeline ran (cache was missed first).
    c.pipeline.run.assert_awaited_once()
    # Cache was populated with the response items, keyed by the canonical
    # URL (query stripped).
    c.result_cache.set.assert_awaited_once()
    cached_url, cached_items = c.result_cache.set.await_args.args
    assert cached_url == "https://www.tiktok.com/@x/video/1"
    assert len(cached_items) == 1
    assert cached_items[0].place.id == "place-uuid-1"
    # Response is the completed envelope.
    assert resp.status == "completed"


@pytest.mark.asyncio
async def test_cache_miss_failed_response_not_cached() -> None:
    """Failed/empty pipeline outcomes don't pollute the cache. The next
    user gets a fresh pipeline run, which may succeed if the failure was
    transient."""
    service, c = _build_service(pipeline_result=[])  # no candidates -> failed
    resp = await service.run(
        raw_input="https://www.tiktok.com/@x/video/1", user_id="u1"
    )
    assert resp.status == "failed"
    c.result_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_skips_pipeline_and_calls_save_places() -> None:
    """Cache hit: skip pipeline + upsert, just link the cached cores to
    the new user."""
    cached = [_cached_item()]
    service, c = _build_service(cache_hit_items=cached)
    resp = await service.run(
        raw_input="https://www.tiktok.com/@x/video/1?web_id=otheruser",
        user_id="u-second",
    )
    # Pipeline never ran.
    c.pipeline.run.assert_not_awaited()
    c.upsert.upsert_and_embed.assert_not_awaited()
    # save_places was called with cached PlaceCores.
    c.user_places.save_places.assert_awaited_once()
    kwargs = c.user_places.save_places.await_args.kwargs
    assert kwargs["user_id"] == "u-second"
    assert kwargs["source_url"] == "https://www.tiktok.com/@x/video/1"
    assert [p.id for p in kwargs["places"]] == ["place-uuid-1"]
    # PlaceSaved fired once for the newly-linked place.
    c.event_dispatcher.dispatch.assert_awaited_once()
    # Response is the same shape as a cache-miss completed response.
    assert resp.status == "completed"
    assert len(resp.results) == 1
    assert resp.results[0].place.place_name == "Fuji Ramen"
    # Status repo still written so the polling route stays consistent.
    c.status_repo.write.assert_awaited_once()
    # We did NOT re-write the cache (it was already there).
    c.result_cache.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_with_duplicates_dispatches_only_for_newly_linked() -> None:
    """When some cached places are already linked for this user,
    save_places raises DuplicateUserPlaceError; the retry filters
    conflicts; PlaceSaved fires only for the genuinely-new links."""
    cached = [
        _cached_item(place_id="place-A", provider_id="google:A", place_name="A"),
        _cached_item(place_id="place-B", provider_id="google:B", place_name="B"),
    ]
    service, c = _build_service(
        cache_hit_items=cached,
        duplicate_place_ids=["place-A"],
    )
    await service.run(raw_input="https://tiktok.com/@x/video/1", user_id="u1")
    # save_places called twice (initial + retry without conflicts).
    assert c.user_places.save_places.await_count == 2
    # PlaceSaved fired once, with only the non-duplicate id.
    c.event_dispatcher.dispatch.assert_awaited_once()
    event = c.event_dispatcher.dispatch.await_args.args[0]
    assert event.place_core_ids == ["place-B"]


@pytest.mark.asyncio
async def test_cache_hit_fk_violation_falls_back_to_pipeline() -> None:
    """A cached PlaceCore.id pointing at a deleted places_v2 row makes
    save_places raise an unhandled error. The service evicts the cache
    entry and falls back to a full pipeline run."""
    cached = [_cached_item()]
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(
        cache_hit_items=cached,
        save_places_first_call_exc=RuntimeError("FK violation: place_id stale"),
        pipeline_result=[candidate],
        upsert_result=[persisted],
    )
    resp = await service.run(raw_input="https://tiktok.com/@x/video/1", user_id="u1")
    # Cache entry was evicted after the bad hit.
    c.result_cache.delete.assert_awaited_once()
    deleted_url = c.result_cache.delete.await_args.args[0]
    assert deleted_url == "https://tiktok.com/@x/video/1"
    # Pipeline then ran (the fallback path).
    c.pipeline.run.assert_awaited_once()
    c.upsert.upsert_and_embed.assert_awaited_once()
    # save_places was called twice: once from the cache path (which
    # raised) and once from the pipeline path.
    assert c.user_places.save_places.await_count == 2
    # The fallback's response is the canonical completed envelope.
    assert resp.status == "completed"
    # The fresh pipeline result is re-cached.
    c.result_cache.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_only_input_does_not_consult_or_write_cache() -> None:
    """Plain-text input has no URL → no cache lookup, no cache write.
    The pipeline runs as normal."""
    candidate = _candidate()
    persisted = _persisted_core()
    service, c = _build_service(pipeline_result=[candidate], upsert_result=[persisted])
    await service.run(raw_input="Fuji Ramen, Tokyo", user_id="u1")
    c.result_cache.get.assert_not_awaited()
    c.result_cache.set.assert_not_awaited()
