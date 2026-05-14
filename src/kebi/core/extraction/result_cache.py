"""ExtractionResultCache — Redis cache of pipeline output keyed by canonical URL.

ADR-074: caches `list[ExtractPlaceItem]` (the per-place picker output,
including evidence and confidence) so the second-and-later users who
share the same TikTok / Instagram / YouTube URL skip the full pipeline
(yt-dlp / Whisper / vision / NER / picker / Google) and just link the
already-extracted PlaceCores to their own `user_places`.

Key: `extract:v1:{sha256(canonical_url)}`. SHA-256 keeps key length
bounded regardless of URL length and avoids Redis key-character pitfalls.
The `v1` prefix lets a future cache-shape change use `extract:v2:`
without colliding.

TTL is injected by the wiring layer from
`config.extraction.result_cache_ttl_seconds` (defaults to 30 days in
`app.yaml`). Long enough to capture viral spread; short enough that
edited/deleted content washes out within a month.

Fail-open: every Redis error degrades the call to a cache miss (read)
or a logged no-op (write). The cache must never take extraction down.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from kebi.api.schemas.extract_place import ExtractPlaceItem

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "extract:v1:"


class ExtractionResultCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, canonical_url: str) -> list[ExtractPlaceItem] | None:
        """Return the cached items for `canonical_url`, or None on miss/error."""
        try:
            raw = await self._redis.get(self._key(canonical_url))
        except Exception:
            logger.exception("extraction_result_cache_get_error")
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return None
            return [ExtractPlaceItem.model_validate(d) for d in data]
        except Exception:
            # Corrupted entry — treat as miss; the next set will overwrite.
            logger.warning(
                "extraction_result_cache_decode_error",
                extra={"canonical_url": canonical_url},
            )
            return None

    async def set(self, canonical_url: str, items: list[ExtractPlaceItem]) -> None:
        """Write `items` to the cache under `canonical_url`. Best-effort."""
        if not items:
            return
        payload = json.dumps([i.model_dump(mode="json") for i in items])
        try:
            await self._redis.set(
                self._key(canonical_url), payload, ex=self._ttl_seconds
            )
        except Exception:
            logger.exception("extraction_result_cache_set_error")

    async def delete(self, canonical_url: str) -> None:
        """Evict the cache entry for `canonical_url`. Missing key is a no-op."""
        try:
            await self._redis.delete(self._key(canonical_url))
        except Exception:
            logger.exception("extraction_result_cache_delete_error")

    @staticmethod
    def _key(canonical_url: str) -> str:
        digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        return f"{_KEY_PREFIX}{digest}"
