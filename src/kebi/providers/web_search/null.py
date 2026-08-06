"""No search backend configured (ADR-145)."""

from __future__ import annotations

import logging

from kebi.providers.web_search.protocol import Freshness, WebResult

logger = logging.getLogger(__name__)


class NullWebSearchProvider:
    """Returns nothing, always.

    Selected when no `BRAVE_API_KEY` is set, which is the state of every
    developer machine that has not opted in and of any deploy that has not
    provisioned the key. The tool is still bound and still callable — it
    simply comes back empty, and `empty_reason` tells the agent to answer
    from what it knows instead of asserting a fact it could not check.

    That degradation is the whole reason the null adapter exists: a missing
    key must cost a nuance, never a 500.
    """

    async def search(
        self,
        query: str,
        *,
        count: int,
        freshness: Freshness | None = None,
        country: str | None = None,
    ) -> list[WebResult]:
        logger.debug("web_search_skipped_no_provider", extra={"query": query})
        return []
