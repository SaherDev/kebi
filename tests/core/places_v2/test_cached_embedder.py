"""Unit tests for CachedEmbedder — Redis-backed query embedding cache."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.core.places_v2.cached_embedder import (
    DEFAULT_TTL_SECONDS,
    CachedEmbedder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedder(
    return_value: list[list[float]] | None = None,
) -> MagicMock:
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=return_value or [[0.1] * 8])
    return embedder


def _make_redis(
    mget_returns: list[Any] | None = None,
    mget_side_effect: Exception | None = None,
    pipeline_set_side_effect: Exception | None = None,
) -> MagicMock:
    redis = MagicMock()
    if mget_side_effect is not None:
        redis.mget = AsyncMock(side_effect=mget_side_effect)
    else:
        redis.mget = AsyncMock(return_value=mget_returns or [])

    pipe = MagicMock()
    pipe.set = MagicMock()
    pipe.execute = AsyncMock()
    if pipeline_set_side_effect is not None:
        pipe.execute = AsyncMock(side_effect=pipeline_set_side_effect)

    pipe_ctx = MagicMock()
    pipe_ctx.__aenter__ = AsyncMock(return_value=pipe)
    pipe_ctx.__aexit__ = AsyncMock(return_value=None)
    redis.pipeline = MagicMock(return_value=pipe_ctx)

    return redis


def _make_cached(
    embedder: MagicMock | None = None,
    redis: MagicMock | None = None,
    model_name: str = "voyage-4-lite",
) -> tuple[CachedEmbedder, MagicMock, MagicMock]:
    e = embedder or _make_embedder()
    r = redis or _make_redis()
    return CachedEmbedder(e, r, model_name), e, r


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


class TestEmpty:
    async def test_empty_texts_returns_empty(self) -> None:
        cached, e, r = _make_cached()
        result = await cached.embed([], "query")
        assert result == []
        e.embed.assert_not_called()
        r.mget.assert_not_called()


# ---------------------------------------------------------------------------
# All-hit, all-miss, mixed
# ---------------------------------------------------------------------------


class TestCacheBehavior:
    async def test_all_hits_skip_underlying_embedder(self) -> None:
        v1 = [0.1] * 8
        v2 = [0.2] * 8
        redis = _make_redis(mget_returns=[json.dumps(v1), json.dumps(v2)])
        cached, e, _ = _make_cached(redis=redis)
        result = await cached.embed(["a", "b"], "query")
        assert result == [v1, v2]
        e.embed.assert_not_called()

    async def test_all_misses_call_embedder_and_write_back(self) -> None:
        embedded = [[0.5] * 8, [0.6] * 8]
        redis = _make_redis(mget_returns=[None, None])
        cached, e, _ = _make_cached(
            embedder=_make_embedder(return_value=embedded),
            redis=redis,
        )
        result = await cached.embed(["a", "b"], "query")
        assert result == embedded
        e.embed.assert_awaited_once_with(["a", "b"], "query")
        # write-back: pipeline executed once, with two SETs
        pipe = redis.pipeline.return_value.__aenter__.return_value
        assert pipe.set.call_count == 2
        pipe.execute.assert_awaited_once()

    async def test_mixed_only_misses_are_embedded(self) -> None:
        cached_vec = [0.1] * 8
        new_vec = [0.9] * 8
        # texts = ["hit", "miss"] → cache returns the cached_vec for index 0,
        # None for index 1.
        redis = _make_redis(mget_returns=[json.dumps(cached_vec), None])
        cached, e, _ = _make_cached(
            embedder=_make_embedder(return_value=[new_vec]),
            redis=redis,
        )
        result = await cached.embed(["hit", "miss"], "query")
        # Order preserved
        assert result == [cached_vec, new_vec]
        # Embedder only called for the miss
        e.embed.assert_awaited_once_with(["miss"], "query")

    async def test_corrupted_cache_entry_treated_as_miss(self) -> None:
        new_vec = [0.7] * 8
        redis = _make_redis(mget_returns=["not valid json"])
        cached, e, _ = _make_cached(
            embedder=_make_embedder(return_value=[new_vec]),
            redis=redis,
        )
        result = await cached.embed(["x"], "query")
        assert result == [new_vec]
        e.embed.assert_awaited_once_with(["x"], "query")


# ---------------------------------------------------------------------------
# Fail-open semantics
# ---------------------------------------------------------------------------


class TestFailOpen:
    async def test_mget_failure_falls_through_to_embedder(self) -> None:
        embedded = [[0.3] * 8, [0.4] * 8]
        redis = _make_redis(mget_side_effect=RuntimeError("redis down"))
        cached, e, _ = _make_cached(
            embedder=_make_embedder(return_value=embedded),
            redis=redis,
        )
        result = await cached.embed(["a", "b"], "query")
        assert result == embedded
        e.embed.assert_awaited_once_with(["a", "b"], "query")

    async def test_set_failure_does_not_break_call(self) -> None:
        embedded = [[0.5] * 8]
        redis = _make_redis(
            mget_returns=[None],
            pipeline_set_side_effect=RuntimeError("redis SET fail"),
        )
        cached, _, _ = _make_cached(
            embedder=_make_embedder(return_value=embedded),
            redis=redis,
        )
        result = await cached.embed(["x"], "query")
        # Vectors still returned despite write-back failure
        assert result == embedded


# ---------------------------------------------------------------------------
# Key construction — model_name + input_type + text all participate
# ---------------------------------------------------------------------------


class TestKeyConstruction:
    def test_key_includes_prefix(self) -> None:
        cached, _, _ = _make_cached()
        key = cached._key("italian", "query")
        assert key.startswith("qembed:")

    def test_key_changes_with_model_name(self) -> None:
        c1, _, _ = _make_cached(model_name="voyage-4-lite")
        c2, _, _ = _make_cached(model_name="voyage-large")
        assert c1._key("italian", "query") != c2._key("italian", "query")

    def test_key_changes_with_input_type(self) -> None:
        cached, _, _ = _make_cached()
        assert (
            cached._key("italian", "query")
            != cached._key("italian", "document")
        )

    def test_key_changes_with_text(self) -> None:
        cached, _, _ = _make_cached()
        assert cached._key("italian", "query") != cached._key("french", "query")

    def test_key_strips_whitespace(self) -> None:
        cached, _, _ = _make_cached()
        # Trivial whitespace doesn't fragment the cache.
        assert (
            cached._key("italian", "query")
            == cached._key("  italian  ", "query")
        )

    def test_key_does_not_lowercase(self) -> None:
        # We deliberately don't lowercase — preserves any case-sensitive
        # semantics in the embedder. "Italian" and "italian" hash distinct.
        cached, _, _ = _make_cached()
        assert (
            cached._key("Italian", "query")
            != cached._key("italian", "query")
        )


# ---------------------------------------------------------------------------
# TTL — defaults to 90 days, overridable per-instance
# ---------------------------------------------------------------------------


class TestTTL:
    def test_default_constant_is_90_days(self) -> None:
        assert DEFAULT_TTL_SECONDS == 90 * 24 * 60 * 60

    async def test_set_uses_default_ttl_when_unspecified(self) -> None:
        redis = _make_redis(mget_returns=[None])
        cached, _, _ = _make_cached(
            embedder=_make_embedder(return_value=[[0.1] * 8]),
            redis=redis,
        )
        await cached.embed(["x"], "query")
        pipe = redis.pipeline.return_value.__aenter__.return_value
        for call in pipe.set.call_args_list:
            assert call.kwargs.get("ex") == DEFAULT_TTL_SECONDS

    async def test_custom_ttl_propagates_to_set(self) -> None:
        redis = _make_redis(mget_returns=[None])
        cached = CachedEmbedder(
            _make_embedder(return_value=[[0.1] * 8]),
            redis,
            "voyage-4-lite",
            ttl_seconds=42,
        )
        await cached.embed(["x"], "query")
        pipe = redis.pipeline.return_value.__aenter__.return_value
        for call in pipe.set.call_args_list:
            assert call.kwargs.get("ex") == 42


# ---------------------------------------------------------------------------
# Exceptions from the underlying embedder propagate
# ---------------------------------------------------------------------------


class TestEmbedderError:
    async def test_embedder_failure_propagates(self) -> None:
        redis = _make_redis(mget_returns=[None])
        embedder = _make_embedder()
        embedder.embed = AsyncMock(side_effect=RuntimeError("voyage 500"))
        cached, _, _ = _make_cached(embedder=embedder, redis=redis)
        with pytest.raises(RuntimeError, match="voyage 500"):
            await cached.embed(["x"], "query")
