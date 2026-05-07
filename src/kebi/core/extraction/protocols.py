"""Protocols for swappable extraction components."""

from typing import Protocol

from kebi.core.extraction.types import ExtractionContext


class Enricher(Protocol):
    """Protocol for cascade enrichers (ADR-038).

    Enrichers populate ExtractionContext fields (caption, candidates, transcript).
    They always return None — results live in context, not in return values.
    External dependencies must be injected via constructor.
    """

    async def enrich(self, context: ExtractionContext) -> None: ...
