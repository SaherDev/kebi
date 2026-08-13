"""Redis-cached web search (ADR-145).

The firing rule for `web_search` is deliberately permissive — the agent calls
it whenever it judges the question needs the outside world, with no
"only after the corpus is thin" gate. That is the right call for coverage and
it makes caching load-bearing rather than an optimisation: without it, ten
users asking the same question in the same week are ten paid lookups of an
answer that did not change.

So the cache key is the *question*, normalised, not the user. Search results
are not personal — nothing user-scoped is in the request and nothing
user-scoped comes back — which is exactly why a global key is safe here and
would not be for anything reading the claims store.

Redis being down degrades to the live provider. A cache that can take the
feature down with it is worse than no cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from kebi.providers.web_search.protocol import Freshness, WebResult, WebSearchProvider

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_PREFIX = "websearch:v1"


def _cache_key(
    query: str, count: int, freshness: Freshness | None, country: str | None
) -> str:
    """Hash the whole request shape, not just the query.

    Two searches differing only in freshness are different questions ("raves
    in Bali" vs "raves in Bali this month"), and serving one for the other is
    how a cache starts telling lies about dates.
    """
    material = json.dumps(
        {
            "q": " ".join(query.lower().split()),
            "n": count,
            "f": freshness or "",
            "c": (country or "").lower(),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return f"{_PREFIX}:{digest}"


class CachedWebSearchProvider:
    """Wraps any `WebSearchProvider` with a shared Redis result cache."""

    def __init__(
        self,
        inner: WebSearchProvider,
        redis: Redis,
        *,
        ttl_seconds: int,
    ) -> None:
        self._inner = inner
        self._redis = redis
        self._ttl = ttl_seconds

    async def search(
        self,
        query: str,
        *,
        count: int,
        freshness: Freshness | None = None,
        country: str | None = None,
    ) -> list[WebResult]:
        key = _cache_key(query, count, freshness, country)
        cached = await self._read(key)
        if cached is not None:
            logger.info("web_search_cache_hit", extra={"query": query})
            return cached

        results = await self._inner.search(
            query, count=count, freshness=freshness, country=country
        )
        # An empty result is not cached. It is usually a provider hiccup or a
        # missing key, and caching it would freeze that outage in for the
        # whole TTL.
        if results:
            await self._write(key, results)
        return results

    async def _read(self, key: str) -> list[WebResult] | None:
        try:
            raw = await self._redis.get(key)
        except Exception:
            logger.warning("web_search_cache_read_failed", exc_info=True)
            return None
        if not raw:
            return None
        try:
            return [WebResult.model_validate(r) for r in json.loads(raw)]
        except Exception:
            # A shape change between deploys should evict, not crash.
            logger.warning("web_search_cache_malformed", extra={"key": key})
            return None

    async def _write(self, key: str, results: list[WebResult]) -> None:
        try:
            await self._redis.set(
                key,
                json.dumps([r.model_dump(mode="json") for r in results]),
                ex=self._ttl,
            )
        except Exception:
            logger.warning("web_search_cache_write_failed", exc_info=True)
