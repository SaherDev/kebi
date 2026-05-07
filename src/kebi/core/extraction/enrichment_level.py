"""EnrichmentLevel — one pass of producers populating shared context."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from kebi.core.extraction.protocols import Enricher
from kebi.core.extraction.types import ExtractionContext

# (context, fired_enricher_names, pick_count) -> human-readable summary
SummaryFn = Callable[[ExtractionContext, list[str], int], str]


@dataclass
class EnrichmentLevel:
    """One level of the extraction cascade.

    A level is a list of pure text/signal-producing enrichers. They
    mutate context (set caption/transcript/title, or append
    `KnownPlace` entries from sources like a vision model). The
    pipeline owns the Search step (`PlacesSearcher`) and the picker
    step (`LLMPlacePicker`) that run after every executed level.

    A level skips entirely when `requires_url=True` and `context.url`
    is `None`. The pipeline calls `level.run(context)` and uses the
    returned `(executed, summary)` to decide whether to run Search +
    pick, emit a step, and persist.
    """

    name: str
    enrichers: list[Enricher]
    summary_fn: SummaryFn
    requires_url: bool = False
    fired: list[str] = field(default_factory=list, init=False, repr=False)

    async def run(self, context: ExtractionContext) -> tuple[bool, list[str]]:
        if self.requires_url and context.url is None:
            return False, []
        fired: list[str] = []
        for enricher in self.enrichers:
            await enricher.enrich(context)
            fired.append(type(enricher).__name__)
        return True, fired
