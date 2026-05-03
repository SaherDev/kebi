"""Unit tests for HybridSearchService — embed + delegate to repo."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.core.places_v2.hybrid_search_service import HybridSearchService
from totoro_ai.core.places_v2.models import (
    HybridSearchFilters,
    HybridSearchHit,
    PlaceCategory,
    PlaceCore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    embed_returns: list[list[float]] | None = None,
    repo_returns: list[HybridSearchHit] | None = None,
) -> tuple[HybridSearchService, MagicMock, MagicMock]:
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=embed_returns or [[0.1] * 1024])
    repo = MagicMock()
    repo.search = AsyncMock(return_value=repo_returns or [])
    return HybridSearchService(repo, embedder), repo, embedder


def _make_hit(pid: str = "p1") -> HybridSearchHit:
    return HybridSearchHit(
        place=PlaceCore(id=pid, place_name="Test", provider_id=f"google:{pid}"),
        user_data=None,
        rrf_score=0.05,
        vector_rank=1,
        text_rank=2,
    )


# ---------------------------------------------------------------------------
# Empty / whitespace queries short-circuit
# ---------------------------------------------------------------------------


class TestEmptyQuery:
    async def test_empty_string_returns_empty_without_embedding(self) -> None:
        service, repo, embedder = _make_service()
        result = await service.search("u1", "")
        assert result == []
        embedder.embed.assert_not_called()
        repo.search.assert_not_called()

    async def test_whitespace_only_returns_empty(self) -> None:
        service, repo, embedder = _make_service()
        result = await service.search("u1", "   \t\n  ")
        assert result == []
        embedder.embed.assert_not_called()
        repo.search.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path: embed → delegate
# ---------------------------------------------------------------------------


class TestEmbedAndDelegate:
    async def test_embedder_called_with_query_input_type(self) -> None:
        service, _, embedder = _make_service()
        await service.search("u1", "italian tokyo")
        embedder.embed.assert_awaited_once_with(
            ["italian tokyo"], input_type="query"
        )

    async def test_repo_receives_first_returned_vector(self) -> None:
        vec = [0.5] * 1024
        service, repo, _ = _make_service(embed_returns=[vec])
        await service.search("u1", "italian")
        kwargs = repo.search.await_args.kwargs
        assert kwargs["query_vector"] == vec

    async def test_repo_receives_cleaned_query_text(self) -> None:
        # Whitespace is trimmed before being sent to both embedder and repo
        # so retrieval doesn't pay for leading/trailing noise.
        service, repo, embedder = _make_service()
        await service.search("u1", "  italian tokyo  ")
        embedder.embed.assert_awaited_once_with(
            ["italian tokyo"], input_type="query"
        )
        kwargs = repo.search.await_args.kwargs
        assert kwargs["query"] == "italian tokyo"

    async def test_user_id_propagates_to_repo(self) -> None:
        service, repo, _ = _make_service()
        await service.search("user-42", "italian")
        kwargs = repo.search.await_args.kwargs
        assert kwargs["user_id"] == "user-42"

    async def test_user_id_none_propagates_for_unscoped_mode(self) -> None:
        service, repo, _ = _make_service()
        await service.search(None, "italian")
        kwargs = repo.search.await_args.kwargs
        assert kwargs["user_id"] is None

    async def test_filters_pass_through_unchanged(self) -> None:
        service, repo, _ = _make_service()
        f = HybridSearchFilters(
            category=PlaceCategory.restaurant,
            tags=["italian"],
            city="Tokyo",
            visited=True,
            saved_after=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await service.search("u1", "italian", filters=f)
        kwargs = repo.search.await_args.kwargs
        assert kwargs["filters"] is f

    async def test_default_knobs_propagate(self) -> None:
        service, repo, _ = _make_service()
        await service.search("u1", "italian")
        kwargs = repo.search.await_args.kwargs
        assert kwargs["limit"] == 20
        assert kwargs["rrf_k"] == 60
        assert kwargs["candidate_multiplier"] == 4

    async def test_custom_knobs_propagate(self) -> None:
        service, repo, _ = _make_service()
        await service.search(
            "u1",
            "italian",
            limit=5,
            rrf_k=120,
            candidate_multiplier=10,
        )
        kwargs = repo.search.await_args.kwargs
        assert kwargs["limit"] == 5
        assert kwargs["rrf_k"] == 120
        assert kwargs["candidate_multiplier"] == 10

    async def test_repo_results_returned_unchanged(self) -> None:
        hits = [_make_hit("a"), _make_hit("b")]
        service, _, _ = _make_service(repo_returns=hits)
        result = await service.search("u1", "italian")
        assert result is hits


# ---------------------------------------------------------------------------
# Error propagation — service doesn't swallow embedder or repo failures
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    async def test_embedder_failure_propagates(self) -> None:
        service, repo, embedder = _make_service()
        embedder.embed = AsyncMock(side_effect=RuntimeError("voyage down"))
        with pytest.raises(RuntimeError, match="voyage down"):
            await service.search("u1", "italian")
        repo.search.assert_not_called()

    async def test_repo_failure_propagates(self) -> None:
        service, repo, _ = _make_service()
        repo.search = AsyncMock(side_effect=RuntimeError("connection lost"))
        with pytest.raises(RuntimeError, match="connection lost"):
            await service.search("u1", "italian")
