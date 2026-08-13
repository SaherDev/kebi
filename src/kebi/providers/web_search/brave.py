"""Brave Search adapter (ADR-145).

Brave runs its own index rather than reselling someone else's, exposes a plain
REST endpoint, and has a free tier that covers development outright. It is
also the choice that keeps the orchestrator swappable: a server-side search
tool bound to one model vendor would work only while the orchestrator is that
vendor, and `config/app.yaml` lists a non-Anthropic orchestrator option today.

Two clusters are read. `web` carries the general index; `news` carries dated,
recent items, which is where match schedules, festival line-ups, and closures
actually live. Merging them here — rather than exposing two methods — keeps
the agent from having to know which cluster a question belongs to.

Every failure path returns `[]`. See the Protocol for why that is the
contract.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from kebi.providers.web_search.protocol import Freshness, WebResult

logger = logging.getLogger(__name__)

_ENDPOINT = "/res/v1/web/search"

# Brave's freshness codes. Our vocabulary is the human word; the wire wants
# the code, and that translation belongs to the adapter, not the agent.
_FRESHNESS_CODES = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

# Brave caps `count` at 20 for the web cluster.
_MAX_COUNT = 20

# Tight budget on purpose: this call sits inside a user-facing turn, and the
# tool's own asyncio timeout is the outer bound. Failing fast leaves room for
# the agent to answer without us.
_TIMEOUT = httpx.Timeout(6.0, connect=3.0)


class BraveWebSearchProvider:
    """Brave Search API — the default backend when a key is configured."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client

    async def search(
        self,
        query: str,
        *,
        count: int,
        freshness: Freshness | None = None,
        country: str | None = None,
    ) -> list[WebResult]:
        params: dict[str, Any] = {
            "q": query,
            "count": max(1, min(count, _MAX_COUNT)),
            "safesearch": "moderate",
            # Ask for the extra sentences around the match. The snippet is the
            # only text the agent ever sees — a one-line description is often
            # too thin to ground a claim on.
            "extra_snippets": "true",
        }
        code = _FRESHNESS_CODES.get(freshness or "")
        if code:
            params["freshness"] = code
        if country and len(country) == 2:
            params["country"] = country.upper()

        try:
            response = await self._client.get(
                f"{self._base_url}{_ENDPOINT}",
                params=params,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.warning("brave_search_failed", extra={"query": query}, exc_info=True)
            return []

        results = _parse(payload)
        logger.info(
            "brave_search_ok",
            extra={"query": query, "results": len(results), "freshness": freshness},
        )
        return results[:count]


def _parse(payload: Any) -> list[WebResult]:
    """Flatten Brave's clusters into one best-first list.

    News leads when present: a dated item answers "when is it" and "is it
    still on", which is most of what brought us here. Deduped by URL, since
    the same page can surface in both clusters.
    """
    if not isinstance(payload, dict):
        return []
    results: list[WebResult] = []
    seen: set[str] = set()
    for cluster in ("news", "web"):
        section = payload.get(cluster)
        if not isinstance(section, dict):
            continue
        for raw in section.get("results") or []:
            item = _to_result(raw)
            if item is None or item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
    return results


def _to_result(raw: Any) -> WebResult | None:
    """One Brave hit → `WebResult`, or None if it carries no usable text."""
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    title = raw.get("title")
    if not isinstance(url, str) or not isinstance(title, str) or not url:
        return None

    # `description` is the matched snippet; `extra_snippets` are the
    # surrounding sentences. Joined, they are what the agent reads.
    parts: list[str] = []
    description = raw.get("description")
    if isinstance(description, str) and description:
        parts.append(description)
    for extra in raw.get("extra_snippets") or []:
        if isinstance(extra, str) and extra:
            parts.append(extra)
    snippet = " ".join(parts).strip()
    if not snippet:
        return None

    meta = raw.get("meta_url")
    site = meta.get("netloc") if isinstance(meta, dict) else None
    age = raw.get("age") or raw.get("page_age")
    return WebResult(
        title=_strip_markup(title),
        url=url,
        snippet=_strip_markup(snippet),
        site=site if isinstance(site, str) else None,
        age=age if isinstance(age, str) else None,
    )


def _strip_markup(text: str) -> str:
    """Brave wraps query terms in <strong>. The agent reads plain text, and
    the chat contract is text plus `kebi://` links only — stray HTML in a
    snippet is one careless quote away from reaching the client."""
    return text.replace("<strong>", "").replace("</strong>", "").strip()
