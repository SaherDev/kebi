"""CachedEmbedder — Redis-backed query embedding cache.

Decorator over any ``EmbedderProtocol``. The wrapped embedder still does
the heavy lifting; this class just spares it from re-embedding the same
text twice.

Per ``embed()`` call:
  1. ``mget`` the cache for every text (key = sha256 of
     ``model_name | input_type | normalized_text``).
  2. Partition into hits + misses.
  3. Embed only the misses via the wrapped embedder.
  4. Write back the new vectors (best-effort pipeline).
  5. Return vectors in the original input order.

Fail-open semantics: a Redis ``mget`` error falls through to the
embedder; a write-back error is logged and swallowed. A flaky cache
must never take search down.

TTL defaults to 90 days. Long enough that genuinely repeated queries
(common phrases the agent generates over weeks) stay hot, short enough
that abandoned keys don't accumulate forever even on Redis instances
without an LRU eviction policy. ``model_name`` is part of the key so
swapping embedders later doesn't poison hits with the wrong vector
space.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from typing import TYPE_CHECKING, cast

from .protocols import EmbedderProtocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "qembed:"

# 90 days — query vectors don't drift for a fixed embedder, but a TTL
# bounds Redis growth on instances without LRU eviction configured.
DEFAULT_TTL_SECONDS: int = 90 * 24 * 60 * 60


@functools.cache
def _shared_redis_client(url: str) -> Redis:
    """Process-wide Redis client per URL — shares the connection pool
    across every CachedEmbedder instance. Mirrors RedisPlacesCache."""
    from redis.asyncio import Redis

    client: Redis = Redis.from_url(url, decode_responses=True)
    return client


class CachedEmbedder:
    def __init__(
        self,
        embedder: EmbedderProtocol,
        redis: Redis,
        model_name: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._embedder = embedder
        self._redis = redis
        self._model_name = model_name
        self._ttl_seconds = ttl_seconds

    @classmethod
    def from_url(
        cls,
        embedder: EmbedderProtocol,
        url: str,
        model_name: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> CachedEmbedder:
        """Construct backed by the shared per-URL Redis client."""
        return cls(embedder, _shared_redis_client(url), model_name, ttl_seconds)

    async def embed(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]:
        if not texts:
            return []

        keys = [self._key(t, input_type) for t in texts]

        try:
            cached_raw = await self._redis.mget(*keys)
        except Exception:
            # Redis down / network blip — bypass cache entirely so search
            # still works. The next successful mget repopulates.
            logger.exception("query_embed_cache_mget_error")
            return await self._embedder.embed(texts, input_type)

        results: list[list[float] | None] = []
        miss_indices: list[int] = []

        for i, raw in enumerate(cached_raw):
            if raw is None:
                results.append(None)
                miss_indices.append(i)
                continue
            try:
                results.append(json.loads(raw))
            except Exception:
                # Corrupted entry — treat as miss so the next embed
                # overwrites it cleanly.
                logger.warning(
                    "query_embed_cache_decode_error",
                    extra={"key": keys[i]},
                )
                results.append(None)
                miss_indices.append(i)

        if miss_indices:
            miss_texts = [texts[i] for i in miss_indices]
            embedded = await self._embedder.embed(miss_texts, input_type)

            for idx, vec in zip(miss_indices, embedded, strict=True):
                results[idx] = vec

            # Write-back is best-effort. A failure here means the next
            # call will miss the cache too — annoying, not fatal.
            try:
                async with self._redis.pipeline(transaction=False) as pipe:
                    for idx, vec in zip(miss_indices, embedded, strict=True):
                        pipe.set(
                            keys[idx],
                            json.dumps(vec),
                            ex=self._ttl_seconds,
                        )
                    await pipe.execute()
            except Exception:
                logger.exception("query_embed_cache_set_error")

        # By construction every slot is now populated (every miss was
        # filled from the embedder result). The cast is a type-narrow,
        # not a runtime check.
        return cast(list[list[float]], results)

    def _key(self, text: str, input_type: str) -> str:
        # strip() folds the most common noise (leading/trailing whitespace
        # from agent-generated queries). Hash includes model_name so a
        # future embedder swap never matches an old key.
        normalized = text.strip()
        digest = hashlib.sha256(
            f"{self._model_name}|{input_type}|{normalized}".encode()
        ).hexdigest()
        return f"{_KEY_PREFIX}{digest}"
