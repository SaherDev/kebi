"""Web-search Protocol — the only surface app code depends on (ADR-145).

Kebi's four existing tools all read kebi's own corpus: saved places, claims,
the place catalog. That is the differentiator, and it is also a ceiling —
a question whose answer is not already in the store has no path to one. The
World Cup schedule, this month's rave line-up, whether the ferry is running,
what a visa costs now: none of that is a place kebi has ingested, and a model
answering from training weights alone is both stale and unfalsifiable.

This is the seam to the outside. `WebResult` is deliberately the small,
provider-neutral shape every search API agrees on — a title, a URL, a snippet,
and (when the provider knows it) how old the page is. Anything richer would
leak one vendor's response schema into the agent layer, which is the thing the
abstraction exists to prevent.

The `age` field is not decoration. Half the questions this closes are
time-sensitive, and an answer that cannot say when a fact was published cannot
be honest about how much to trust it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

# How fresh a result set must be. Maps to the provider's own freshness
# parameter; `None` means no constraint. Kept as a small controlled set
# rather than raw date ranges — the agent picks a bucket, not a syntax.
Freshness = str  # one of: "day" | "week" | "month" | "year" | None


class WebResult(BaseModel):
    """One search hit, in provider-neutral terms."""

    model_config = ConfigDict(frozen=True)

    title: str
    url: str
    snippet: str
    # Publisher domain ("bbc.com"), for attribution in the answer.
    site: str | None = None
    # Provider-reported age of the page ("2 days ago", "March 4, 2026").
    # Free text on purpose: providers disagree on format and normalising it
    # to a datetime would invent precision none of them guarantee.
    age: str | None = None


@runtime_checkable
class WebSearchProvider(Protocol):
    """A search backend. Implementations must never raise."""

    async def search(
        self,
        query: str,
        *,
        count: int,
        freshness: Freshness | None = None,
        country: str | None = None,
    ) -> list[WebResult]:
        """Return up to `count` results, best first, or `[]`.

        `country` is an ISO-3166 alpha-2 code used to localise the index —
        "where can I watch the match" wants Indonesian results when the user
        is in Bali, not American ones.

        Returning `[]` rather than raising is the contract, not laziness.
        Search is enrichment layered on an answer the agent can already give
        from its own knowledge and kebi's claims; a provider outage should
        cost the answer some freshness, never the turn.
        """
        ...
