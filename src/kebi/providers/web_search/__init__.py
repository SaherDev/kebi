"""Provider-agnostic web search.

`WebSearchProvider` is the seam between the agent and whichever search index
happens to be wired in. Concrete adapters live alongside in this package;
`CachedWebSearchProvider` composes over any of them.

The surface is one method on purpose. Everything the agent needs from the
outside world — a schedule, a price, an event date, a norm — is a query and
some results; a provider-shaped API of `news()` / `places()` / `images()`
would push routing decisions into the prompt, which is the budget ADR-137
spent moving *out* of it.
"""

from __future__ import annotations

from kebi.providers.web_search.brave import BraveWebSearchProvider
from kebi.providers.web_search.cached import CachedWebSearchProvider
from kebi.providers.web_search.null import NullWebSearchProvider
from kebi.providers.web_search.protocol import (
    Freshness,
    WebResult,
    WebSearchProvider,
)

__all__ = [
    "BraveWebSearchProvider",
    "CachedWebSearchProvider",
    "Freshness",
    "NullWebSearchProvider",
    "WebResult",
    "WebSearchProvider",
]
