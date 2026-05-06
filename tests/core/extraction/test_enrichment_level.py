"""Tests for EnrichmentLevel — the producer-runner unit."""

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
from totoro_ai.core.extraction.types import ExtractionContext


def _summary(_ctx: ExtractionContext, _fired: list[str], _picks: int) -> str:
    return ""


async def test_run_executes_each_enricher() -> None:
    """All enrichers in the level run in order against the shared context."""
    e1 = MagicMock()
    e1.enrich = AsyncMock()
    e2 = MagicMock()
    e2.enrich = AsyncMock()

    level = EnrichmentLevel(
        name="enrich", enrichers=[e1, e2], summary_fn=_summary
    )
    ctx = ExtractionContext(url=None, user_id="u1", supplementary_text="...")

    executed, fired = await level.run(ctx)

    assert executed is True
    e1.enrich.assert_awaited_once_with(ctx)
    e2.enrich.assert_awaited_once_with(ctx)
    assert fired == [type(e1).__name__, type(e2).__name__]


async def test_requires_url_skips_when_url_none() -> None:
    """A URL-only level must not call enrichers when url is None."""
    e1 = MagicMock()
    e1.enrich = AsyncMock()

    level = EnrichmentLevel(
        name="deep",
        enrichers=[e1],
        summary_fn=_summary,
        requires_url=True,
    )
    ctx = ExtractionContext(url=None, user_id="u1")

    executed, fired = await level.run(ctx)

    assert executed is False
    assert fired == []
    e1.enrich.assert_not_called()


async def test_requires_url_runs_when_url_present() -> None:
    e1 = MagicMock()
    e1.enrich = AsyncMock()

    level = EnrichmentLevel(
        name="deep",
        enrichers=[e1],
        summary_fn=_summary,
        requires_url=True,
    )
    ctx = ExtractionContext(url="https://tiktok.com/x", user_id="u1")

    executed, _ = await level.run(ctx)

    assert executed is True
    e1.enrich.assert_awaited_once_with(ctx)


async def test_run_returns_fired_enricher_class_names() -> None:
    """level.run returns the list of enricher class names that ran."""

    class FooEnricher:
        async def enrich(self, _ctx: ExtractionContext) -> None: ...

    class BarEnricher:
        async def enrich(self, _ctx: ExtractionContext) -> None: ...

    level = EnrichmentLevel(
        name="x",
        enrichers=[FooEnricher(), BarEnricher()],
        summary_fn=_summary,
    )
    _, fired = await level.run(ExtractionContext(url=None, user_id="u1"))

    assert fired == ["FooEnricher", "BarEnricher"]
