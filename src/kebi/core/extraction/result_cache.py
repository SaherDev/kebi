"""ExtractionResultCache — Redis cache of pipeline output keyed by (source, source_ref).

ADR-074: caches `list[ExtractPlaceItem]` (the per-place picker output,
including evidence and confidence) so the second-and-later users who
share the same content-addressable reference skip the full pipeline
(yt-dlp / Whisper / vision / NER / picker / Google) and just link the
already-extracted PlaceCores to their own `user_places`.

Key shape: `extract:v2:{source.value}:{sha256(source_ref)}`. The
`source` segment namespaces by `PlaceSource` so cacheable refs from
different platforms never share a slot — today that's TikTok /
Instagram / YouTube / Google Maps lists keyed by canonical URL, but
the same shape supports a future kebi-internal ref (e.g. a share
token) whose `source_ref` isn't a URL at all. SHA-256 keeps key
length bounded and avoids Redis key-character pitfalls. The version
prefix isolates output-shape/content changes: `v1` → `v2` when
ADR-118 strengthened tag emission (old v1 entries age out via TTL).

Cacheability is a per-source decision the service makes before calling
in: `manual` freetext isn't content-addressable and is never cached;
URL-bearing sources cache by canonical URL. The cache itself just
trusts whatever `(source, source_ref)` pair the service hands it.

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
from kebi.core.places import PlaceSource

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "extract:v2:"


class ExtractionResultCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(
        self, source: PlaceSource, source_ref: str
    ) -> list[ExtractPlaceItem] | None:
        """Return the cached items for `(source, source_ref)`, or None on miss/error."""
        try:
            raw = await self._redis.get(self._key(source, source_ref))
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
                extra={"source": source.value, "source_ref": source_ref},
            )
            return None

    async def set(
        self,
        source: PlaceSource,
        source_ref: str,
        items: list[ExtractPlaceItem],
    ) -> None:
        """Write `items` to the cache under `(source, source_ref)`. Best-effort."""
        if not items:
            return
        payload = json.dumps([i.model_dump(mode="json") for i in items])
        try:
            await self._redis.set(
                self._key(source, source_ref), payload, ex=self._ttl_seconds
            )
        except Exception:
            logger.exception("extraction_result_cache_set_error")

    async def delete(self, source: PlaceSource, source_ref: str) -> None:
        """Evict the cache entry for `(source, source_ref)`. Missing key is a no-op."""
        try:
            await self._redis.delete(self._key(source, source_ref))
        except Exception:
            logger.exception("extraction_result_cache_delete_error")

    @staticmethod
    def _key(source: PlaceSource, source_ref: str) -> str:
        digest = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()
        return f"{_KEY_PREFIX}{source.value}:{digest}"
