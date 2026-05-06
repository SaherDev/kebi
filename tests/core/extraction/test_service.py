"""Tests for ExtractionService (ADR-054 / feature 019 / ADR-063 / feature 027 M1)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.api.schemas.extract_place import ExtractPlaceResponse
from totoro_ai.core.extraction.persistence import PlaceSaveOutcome
from totoro_ai.core.extraction.service import ExtractionService
from totoro_ai.core.extraction.types import (
    Evidence,
    Medium,
    Producer,
    ValidatedCandidate,
)
from totoro_ai.core.places import (
    PlaceAttributes,
    PlaceObject,
    PlaceProvider,
    PlaceType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validated(
    place_name: str = "Fuji Ramen",
    external_id: str = "place_123",
    confidence: float = 0.87,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.google,
        external_id=external_id,
        confidence=confidence,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
        subcategory="restaurant",
        attributes=PlaceAttributes(cuisine="ramen"),
    )


def _make_place_object(
    place_id: str = "place-uuid-1",
    place_name: str = "Fuji Ramen",
) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name=place_name,
        place_type=PlaceType.food_and_drink,
        subcategory="restaurant",
        attributes=PlaceAttributes(cuisine="ramen"),
        provider_id="google:place_123",
    )


def _saved_outcome(
    validated: ValidatedCandidate | None = None, place_id: str = "place-uuid-1"
) -> PlaceSaveOutcome:
    vc = validated or _make_validated()
    return PlaceSaveOutcome(
        metadata=vc,
        place=_make_place_object(place_id=place_id, place_name=vc.place_name),
        place_id=place_id,
        status="saved",
    )


def _below_threshold_outcome(
    validated: ValidatedCandidate | None = None,
) -> PlaceSaveOutcome:
    vc = validated or _make_validated(confidence=0.20)
    return PlaceSaveOutcome(
        metadata=vc, place=None, place_id=None, status="below_threshold"
    )


@pytest.fixture
def pipeline() -> MagicMock:
    p = MagicMock()
    p.run = AsyncMock(return_value=[_make_validated()])
    return p


@pytest.fixture
def persistence() -> MagicMock:
    ps = MagicMock()
    ps.save_and_emit = AsyncMock(return_value=[_saved_outcome()])
    return ps


@pytest.fixture
def status_repo() -> MagicMock:
    sr = MagicMock()
    sr.write = AsyncMock()
    return sr


@pytest.fixture
def service(
    pipeline: MagicMock,
    persistence: MagicMock,
    status_repo: MagicMock,
) -> ExtractionService:
    return ExtractionService(
        pipeline=pipeline, persistence=persistence, status_repo=status_repo
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_empty_raw_input_raises_value_error(service: ExtractionService) -> None:
    with pytest.raises(ValueError, match="raw_input cannot be empty"):
        await service.run("", user_id="user-1")


async def test_whitespace_only_raw_input_raises_value_error(
    service: ExtractionService,
) -> None:
    with pytest.raises(ValueError, match="raw_input cannot be empty"):
        await service.run("   ", user_id="user-1")


# ---------------------------------------------------------------------------
# Inline await (M1) — run() returns terminal envelope synchronously
# ---------------------------------------------------------------------------


async def test_run_returns_extract_place_response(service: ExtractionService) -> None:
    response = await service.run("Fuji Ramen Bangkok", user_id="user-1")
    assert isinstance(response, ExtractPlaceResponse)


async def test_run_never_returns_pending_inline(service: ExtractionService) -> None:
    """ADR-063 + M1: run() never returns 'pending' — that's the route layer's job."""
    response = await service.run("Fuji Ramen Bangkok", user_id="user-1")
    assert response.status in ("completed", "failed")


async def test_run_returns_completed_envelope_with_real_items(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
) -> None:
    results = [_make_validated()]
    pipeline.run = AsyncMock(return_value=results)
    persistence.save_and_emit = AsyncMock(return_value=[_saved_outcome(results[0])])

    response = await service.run("Fuji Ramen", user_id="user-1")

    assert response.status == "completed"
    assert len(response.results) == 1
    assert response.results[0].status == "saved"
    assert response.results[0].place is not None
    assert response.results[0].place.place_name == "Fuji Ramen"
    assert response.results[0].confidence == pytest.approx(0.87)


async def test_run_pipeline_empty_returns_failed(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
) -> None:
    pipeline.run = AsyncMock(return_value=[])

    response = await service.run("https://tiktok.com/v/abc", user_id="user-1")

    persistence.save_and_emit.assert_not_awaited()
    assert response.status == "failed"
    assert response.results == []


async def test_run_all_below_threshold_returns_failed(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
) -> None:
    """All below-threshold -> envelope status=failed, results=[]. No null items."""
    vc = _make_validated(confidence=0.20)
    pipeline.run = AsyncMock(return_value=[vc])
    persistence.save_and_emit = AsyncMock(return_value=[_below_threshold_outcome(vc)])

    response = await service.run("The Coffee Shop", user_id="user-1")

    assert response.status == "failed"
    assert response.results == []


async def test_run_mixed_above_and_below_threshold_filters_below(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
) -> None:
    """Mixed outcomes: above-threshold stays, below-threshold dropped."""
    vc_ok = _make_validated(place_name="Saved Place", external_id="save_1")
    vc_bad = _make_validated(
        place_name="Weak Place", external_id="weak_1", confidence=0.20
    )
    pipeline.run = AsyncMock(return_value=[vc_ok, vc_bad])
    persistence.save_and_emit = AsyncMock(
        return_value=[_saved_outcome(vc_ok), _below_threshold_outcome(vc_bad)]
    )

    response = await service.run("mix", user_id="user-1")

    assert response.status == "completed"
    assert len(response.results) == 1
    assert response.results[0].place is not None
    assert response.results[0].place.place_name == "Saved Place"


async def test_raw_input_echoed_verbatim(service: ExtractionService) -> None:
    """raw_input is the exact bytes submitted — no normalization (ADR-063)."""
    gnarly = "  https://tiktok.com/v/abc?utm=spam   "
    response = await service.run(gnarly, user_id="user-1")
    assert response.raw_input == gnarly


async def test_run_uses_caller_request_id(
    service: ExtractionService, status_repo: MagicMock
) -> None:
    """When caller injects request_id, envelope and Redis write both use it."""
    response = await service.run(
        "Fuji Ramen", user_id="user-1", request_id="rid_caller"
    )
    assert response.request_id == "rid_caller"
    status_repo.write.assert_awaited_once()
    assert status_repo.write.call_args.args[0] == "rid_caller"


async def test_run_generates_request_id_when_caller_omits(
    service: ExtractionService,
) -> None:
    response = await service.run("Fuji Ramen Bangkok", user_id="user-1")
    assert response.request_id is not None
    assert len(response.request_id) == 32


async def test_run_writes_redis_envelope(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
    status_repo: MagicMock,
) -> None:
    """run() writes the envelope to Redis on the critical path (FR-010)."""
    results = [_make_validated()]
    pipeline.run = AsyncMock(return_value=results)
    persistence.save_and_emit = AsyncMock(return_value=[_saved_outcome(results[0])])

    response = await service.run("Fuji Ramen", user_id="user-1")

    status_repo.write.assert_awaited_once()
    payload = status_repo.write.call_args.args[1]
    assert payload["status"] == "completed"
    assert payload["results"][0]["status"] == "saved"
    assert payload["raw_input"] == "Fuji Ramen"
    assert payload["request_id"] == response.request_id


async def test_run_pipeline_exception_degrades_to_failed(
    service: ExtractionService,
    pipeline: MagicMock,
    status_repo: MagicMock,
) -> None:
    """Pipeline raising collapses to envelope status=failed (does not propagate)."""
    pipeline.run = AsyncMock(side_effect=RuntimeError("whisper timeout"))

    response = await service.run("Fuji Ramen", user_id="user-1")

    assert response.status == "failed"
    assert response.results == []
    status_repo.write.assert_awaited_once()
    payload = status_repo.write.call_args.args[1]
    assert payload["status"] == "failed"


async def test_tiktok_url_stamps_source(
    service: ExtractionService,
    pipeline: MagicMock,
    persistence: MagicMock,
) -> None:
    from totoro_ai.core.places import PlaceSource

    results = [_make_validated()]
    pipeline.run = AsyncMock(return_value=results)
    persistence.save_and_emit = AsyncMock(return_value=[_saved_outcome(results[0])])

    url = "https://www.tiktok.com/@user/video/123"
    await service.run(url, user_id="user-1")

    kw = persistence.save_and_emit.call_args.kwargs
    assert kw["source"] == PlaceSource.tiktok
    assert kw["source_url"] == url


async def test_pipeline_called_with_parsed_args(
    service: ExtractionService, pipeline: MagicMock
) -> None:
    await service.run("https://tiktok.com/v/999 great ramen", user_id="user-42")
    pipeline.run.assert_awaited_once()
    kw = pipeline.run.call_args.kwargs
    assert kw["url"] == "https://tiktok.com/v/999"
    assert kw["supplementary_text"] == "great ramen"
    assert kw["user_id"] == "user-42"


async def test_pipeline_called_with_url_none_for_plain_text(
    service: ExtractionService, pipeline: MagicMock
) -> None:
    await service.run("Fuji Ramen no url", user_id="user-1")
    kw = pipeline.run.call_args.kwargs
    assert kw["url"] is None


# ---------------------------------------------------------------------------
# Unsupported-URL early exit
# ---------------------------------------------------------------------------


async def test_unsupported_url_returns_failed_without_running_pipeline(
    service: ExtractionService, pipeline: MagicMock
) -> None:
    """A URL whose host isn't a supported source must short-circuit before
    the pipeline runs. The user gets a clear `save.unsupported_url`
    reasoning step instead of the cascade-timeout cliff."""
    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    response = await service.run(
        "https://example.com/some-blog-post", user_id="u1", emit=spy
    )

    assert response.status == "failed"
    assert response.results == []
    pipeline.run.assert_not_called()
    steps = [s for s, _ in emitted]
    assert "save.unsupported_url" in steps
    msg = next(m for s, m in emitted if s == "save.unsupported_url")
    assert "TikTok" in msg and "Instagram" in msg
    assert "YouTube" in msg and "Google Maps" in msg


# ---------------------------------------------------------------------------
# emit callback contract (feature 028 M4)
# ---------------------------------------------------------------------------


async def test_emit_fires_parse_input_and_persist(service: ExtractionService) -> None:
    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.run("Fuji Ramen Bangkok", user_id="user-1", emit=spy)

    steps = [s for s, _ in emitted]
    assert steps[0] == "save.parse_input"
    assert steps[-1] == "save.persist"


async def test_emit_passes_callback_through_to_pipeline(
    service: ExtractionService, pipeline: MagicMock
) -> None:
    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    await service.run("Fuji Ramen Bangkok", user_id="user-1", emit=spy)

    assert pipeline.run.await_count == 1
    # The pipeline receives the same emit callable — callers in M5 rely on this.
    assert pipeline.run.await_args.kwargs.get("emit") is spy
