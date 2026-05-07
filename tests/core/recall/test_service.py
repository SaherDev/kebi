"""Tests for the recall service (feature 019 shape).

The service now drives:

1. `repo.search(...)` → `(list[RecallResult(place=PlaceObject)], int)`
2. `places_service.enrich_batch(places, geo_only=True)` for Tier 2 attach
3. Optional Python-side haversine distance filter
4. `RecallResponse(results, total_count, empty_state)`
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kebi.api.schemas.recall import RecallResponse
from kebi.core.config import RecallConfig
from kebi.core.places.models import PlaceObject, PlaceType
from kebi.core.recall.service import RecallService
from kebi.core.recall.types import RecallFilters
from kebi.core.recall.types import RecallResult as InternalRecallResult
from kebi.providers.embeddings import EmbedderProtocol


@pytest.fixture
def recall_config() -> RecallConfig:
    return RecallConfig(max_results=10, rrf_k=60, candidate_multiplier=2)


@pytest.fixture
def mock_embedder() -> AsyncMock:
    return AsyncMock(spec=EmbedderProtocol)


@pytest.fixture
def mock_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_places_service() -> AsyncMock:
    svc = AsyncMock()
    svc.enrich_batch = AsyncMock(side_effect=lambda places, geo_only=False: places)
    return svc


@pytest.fixture
def recall_service(
    mock_embedder: AsyncMock,
    mock_repo: AsyncMock,
    recall_config: RecallConfig,
    mock_places_service: AsyncMock,
) -> RecallService:
    return RecallService(
        embedder=mock_embedder,
        recall_repo=mock_repo,
        config=recall_config,
        places_service=mock_places_service,
    )


def _make_place(
    place_id: str = "p1", lat: float | None = None, lng: float | None = None
) -> PlaceObject:
    return PlaceObject(
        place_id=place_id,
        place_name="Cafe A",
        place_type=PlaceType.food_and_drink,
        lat=lat,
        lng=lng,
        geo_fresh=lat is not None,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRecallServiceHappyPath:
    async def test_run_with_embedding_and_search_returns_recall_response(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        mock_embedder.embed.return_value = [[0.1, 0.2, 0.3]]
        mock_repo.count_saved_places.return_value = 5
        mock_repo.search.return_value = (
            [
                InternalRecallResult(
                    place=_make_place("p1"),
                    match_reason="semantic + keyword",
                    relevance_score=0.8,
                ),
            ],
            1,
        )

        response = await recall_service.run("cosy ramen spot", "test-user-1")

        assert isinstance(response, RecallResponse)
        assert response.total_count == 1
        assert response.empty_state is False
        assert response.results[0].place.place_id == "p1"
        assert response.results[0].match_reason == "semantic + keyword"
        assert response.results[0].relevance_score == 0.8
        mock_embedder.embed.assert_awaited_once_with(
            ["cosy ramen spot"], input_type="query"
        )

    async def test_emit_callback_fires_mode_and_result_for_hybrid(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        mock_embedder.embed.return_value = [[0.1]]
        mock_repo.count_saved_places.return_value = 3
        mock_repo.search.return_value = (
            [InternalRecallResult(place=_make_place("p1"), match_reason="semantic")],
            1,
        )
        emitted: list[tuple[str, str]] = []

        def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
            emitted.append((step, summary))

        await recall_service.run("ramen", "u1", emit=spy)

        steps = [s for s, _ in emitted]
        assert steps == ["recall.mode", "recall.result"]
        assert "Searching for 'ramen' in your saves" in emitted[0][1]
        assert "1 places matched" in emitted[1][1]

    async def test_emit_callback_fires_for_filter_mode(
        self,
        recall_service: RecallService,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.count_saved_places.return_value = 3
        mock_repo.search.return_value = ([], 0)
        emitted: list[tuple[str, str]] = []

        def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
            emitted.append((step, summary))

        await recall_service.run(None, "u1", emit=spy)

        assert [s for s, _ in emitted] == ["recall.mode", "recall.result"]
        assert "Browsing your saves" in emitted[0][1]

    async def test_run_passes_limit_and_rrf_k_to_repo(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
        recall_config: RecallConfig,
    ) -> None:
        mock_embedder.embed.return_value = [[0.1]]
        mock_repo.count_saved_places.return_value = 20
        mock_repo.search.return_value = ([], 0)

        await recall_service.run("test query", "test-user")

        call = mock_repo.search.await_args
        assert call.kwargs["limit"] == recall_config.max_results
        assert call.kwargs["rrf_k"] == 60

    async def test_empty_string_query_normalized_to_none_for_filter_mode(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        """An empty or whitespace-only query normalizes to None so the
        repository dispatches to filter-mode (ADR-057 follow-up). The
        embedder is NOT called for filter-mode queries."""
        mock_repo.count_saved_places.return_value = 5
        mock_repo.search.return_value = ([], 0)

        await recall_service.run("   ", "test-user")

        mock_embedder.embed.assert_not_called()
        call = mock_repo.search.await_args
        assert call.kwargs["query"] is None
        assert call.kwargs["query_vector"] is None


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


class TestRecallServiceColdStart:
    async def test_zero_saves_returns_empty_state_true(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        mock_repo.count_saved_places.return_value = 0

        response = await recall_service.run("any query", "new-user")

        assert response.results == []
        assert response.total_count == 0
        assert response.empty_state is True
        mock_embedder.embed.assert_not_called()
        mock_repo.search.assert_not_called()


# ---------------------------------------------------------------------------
# Embedding failure
# ---------------------------------------------------------------------------


class TestRecallServiceEmbeddingFailure:
    async def test_embedding_failure_falls_back_to_text_only(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        mock_embedder.embed.side_effect = RuntimeError("API timeout")
        mock_repo.count_saved_places.return_value = 3
        mock_repo.search.return_value = (
            [
                InternalRecallResult(
                    place=_make_place("p1"),
                    match_reason="keyword",
                    relevance_score=0.2,
                ),
            ],
            1,
        )

        response = await recall_service.run("ramen", "test-user")

        assert response.results[0].match_reason == "keyword"
        assert response.empty_state is False
        call = mock_repo.search.await_args
        assert call.kwargs["query_vector"] is None
        assert call.kwargs["query"] == "ramen"

    async def test_embedding_failure_does_not_raise_to_caller(
        self,
        recall_service: RecallService,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        mock_embedder.embed.side_effect = RuntimeError("Network")
        mock_repo.count_saved_places.return_value = 1
        mock_repo.search.return_value = ([], 0)

        response = await recall_service.run("query", "user")

        assert isinstance(response, RecallResponse)
        assert response.results == []


# ---------------------------------------------------------------------------
# Distance filter — post-enrichment, service-side
# ---------------------------------------------------------------------------


class TestRecallServiceDistanceFilter:
    async def test_haversine_filter_drops_far_places_when_threshold_set(
        self,
        mock_embedder: AsyncMock,
        mock_repo: AsyncMock,
        recall_config: RecallConfig,
    ) -> None:
        near = _make_place("near", lat=13.7563, lng=100.5018)
        far = _make_place("far", lat=14.0, lng=101.5)  # ~100+ km away
        mock_repo.count_saved_places.return_value = 2
        mock_repo.search.return_value = (
            [
                InternalRecallResult(
                    place=near, match_reason="filter", relevance_score=None
                ),
                InternalRecallResult(
                    place=far, match_reason="filter", relevance_score=None
                ),
            ],
            2,
        )

        places_service = AsyncMock()
        places_service.enrich_batch = AsyncMock(
            side_effect=lambda places, geo_only=False: places
        )
        svc = RecallService(
            embedder=mock_embedder,
            recall_repo=mock_repo,
            config=recall_config,
            places_service=places_service,
        )

        response = await svc.run(
            query=None,
            user_id="u1",
            filters=RecallFilters(max_distance_km=5),
            location=(13.7563, 100.5018),
        )

        # Only the near one survives the distance filter.
        assert [r.place.place_id for r in response.results] == ["near"]
        # Total count is the best-effort DB-side count, unchanged.
        assert response.total_count == 2
