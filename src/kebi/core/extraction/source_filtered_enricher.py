"""Base class for enrichers that only run for specific `PlaceSource`s."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from kebi.core.extraction.types import ExtractionContext
from kebi.core.places_v2 import PlaceSource


class SourceFilteredEnricher(ABC):
    """Enricher base that short-circuits unsupported `PlaceSource`s.

    Subclasses declare which sources they handle by passing
    `allowed_sources` to `super().__init__()`. The base's `enrich`
    runs `supports(context)` first; when it returns `False`, the
    subclass's `_run` never executes — no network call, no subprocess,
    no circuit-breaker false failure for guaranteed-mismatch URLs.
    """

    def __init__(self, allowed_sources: Iterable[PlaceSource]) -> None:
        self._allowed_sources: frozenset[PlaceSource] = frozenset(allowed_sources)

    @property
    def allowed_sources(self) -> frozenset[PlaceSource]:
        return self._allowed_sources

    def supports(self, context: ExtractionContext) -> bool:
        return context.source in self._allowed_sources

    async def enrich(self, context: ExtractionContext) -> None:
        if not self.supports(context):
            return
        # `source` is auto-derived from `url` (ExtractionContext.__post_init__),
        # so any non-None source guarantees a URL is present. Subclasses can
        # rely on `context.url` being non-None inside `_run`.
        assert context.url is not None
        await self._run(context)

    @abstractmethod
    async def _run(self, context: ExtractionContext) -> None:
        """Subclass-specific enrichment. URL guaranteed present when called."""
