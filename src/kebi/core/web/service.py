"""WebKnowledgeService — the outside world, shaped for an answer (ADR-145).

Between the raw provider and the agent sit three jobs, all of which exist
because a search result is not a fact:

*Localisation.* "where can I watch the final" has a different answer in
Canggu than in Chicago. The turn already resolved a working location, so the
country goes to the provider and the area name goes into the query. The agent
never passes a location — one less argument to get wrong, and one less place
for the conversation's location to drift from the search's.

*Trimming.* Snippets arrive long and repetitive. Every character competes for
attention with the claims, which are the part of an answer that is ours, so
the text is capped and near-duplicates are dropped.

*Naming the empty case.* A search that found nothing and a search that could
not run are different outcomes, and the difference is the one thing standing
between "I couldn't find anything current on that" and a confidently wrong
date.
"""

from __future__ import annotations

import logging

from kebi.core.agent.location import WorkingLocation
from kebi.core.config import WebSearchToolConfig
from kebi.core.web.models import WebFinding, WebSearchResult
from kebi.providers.web_search import Freshness, WebResult, WebSearchProvider

logger = logging.getLogger(__name__)


class WebKnowledgeService:
    """Runs a localised, trimmed web search for the agent."""

    def __init__(
        self,
        provider: WebSearchProvider,
        config: WebSearchToolConfig,
    ) -> None:
        self._provider = provider
        self._config = config

    async def search(
        self,
        *,
        query: str,
        freshness: Freshness | None = None,
        limit: int | None = None,
        working: WorkingLocation | None = None,
    ) -> WebSearchResult:
        count = min(limit or self._config.default_limit, self._config.max_limit)
        localised = _localise(query, working)
        country = (working.country_code if working else None) or None

        results = await self._provider.search(
            localised, count=count, freshness=freshness, country=country
        )
        findings = _to_findings(results, self._config.snippet_max_chars)

        area = {
            "country_code": working.country_code if working else None,
            "city": working.city if working else None,
            "neighborhood": working.neighborhood if working else None,
        }
        if not findings:
            logger.info(
                "web_search_empty", extra={"query": localised, "country": country}
            )
            return WebSearchResult(
                query=localised,
                empty_reason="no_results",
                **area,
            )
        return WebSearchResult(query=localised, findings=findings, **area)


def _localise(query: str, working: WorkingLocation | None) -> str:
    """Append the area to the query when the question is clearly local.

    Only when the query names no part of the area already. The agent usually
    writes "world cup 2026 group stage schedule", where a place name would
    narrow the search to nothing, but writes "atm fees", where it is the whole
    point.

    "Names no part" means *any* level, not just the most specific one. Walking
    the levels and appending the first one absent is the obvious
    implementation and it is wrong: "canggu atm fees" would pick up the
    administrative city and search "canggu atm fees Badung", which is a worse
    query than either name alone — nobody writing about Canggu says Badung.
    """
    if working is None:
        return query
    haystack = query.casefold()
    names = [working.neighborhood, working.city, working.country]
    if any(name and name.casefold() in haystack for name in names):
        return query
    for name in names:
        if name:
            return f"{query} {name}"
    return query


def _to_findings(results: list[WebResult], max_chars: int) -> list[WebFinding]:
    """Trim, deduplicate, and attribute."""
    findings: list[WebFinding] = []
    seen: set[str] = set()
    for result in results:
        text = _trim(f"{result.title}. {result.snippet}", max_chars)
        # Fingerprint on the leading words: syndicated stories repeat the
        # same lede across a dozen domains, and three copies of one fact
        # reads to the model as three sources agreeing.
        fingerprint = " ".join(text.casefold().split()[:12])
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        findings.append(
            WebFinding(text=text, source=result.site, age=result.age, url=result.url)
        )
    return findings


def _trim(text: str, max_chars: int) -> str:
    """Cap at a word boundary, so a finding never ends mid-word."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{clipped}…"
