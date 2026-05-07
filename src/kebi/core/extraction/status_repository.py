"""ExtractionStatusRepository — cache-backed status store for deferred extractions."""

from __future__ import annotations

import json
import logging
from typing import Any

from kebi.providers.cache import CacheBackend

logger = logging.getLogger(__name__)

# ADR-063: bumped to v2 alongside the two-level ExtractPlaceResponse shape.
# Legacy `extraction:*` keys are intentionally unread — polling returns 404
# for them (same path as TTL expiry).
_KEY_PREFIX = "extraction:v2"
_DEFAULT_TTL = 3600


class ExtractionStatusRepository:
    """Read/write extraction status from a CacheBackend (ADR-038, ADR-063).

    Key format: extraction:v2:{request_id}
    TTL: 3600s (1 hour) by default.
    Value: JSON-serialized `ExtractPlaceResponse` envelope per ADR-063.
    """

    def __init__(self, cache: CacheBackend) -> None:
        self._cache = cache

    async def write(
        self, request_id: str, payload: dict[str, Any], ttl: int = _DEFAULT_TTL
    ) -> None:
        """Serialize payload to JSON and write to cache with TTL."""
        key = f"{_KEY_PREFIX}:{request_id}"
        await self._cache.set(key, json.dumps(payload), ttl)

    async def read(self, request_id: str) -> dict[str, Any] | None:
        """Read and deserialize cached payload. Returns None if key missing."""
        key = f"{_KEY_PREFIX}:{request_id}"
        raw = await self._cache.get(key)
        if raw is None:
            return None
        return json.loads(raw)  # type: ignore[no-any-return]
