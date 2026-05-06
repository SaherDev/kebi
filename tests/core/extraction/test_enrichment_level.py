"""Tests for EnrichmentLevel — the (enrich → dedup → summarize) unit."""

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ExtractionContext,
    ExtractionLevel,
)
from totoro_ai.core.places import (
    PlaceAttributes,
    PlaceType,
)


def _candidate(name: str = "Place") -> CandidatePlace:
    return CandidatePlace(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        source=ExtractionLevel.LLM_NER,
        attributes=PlaceAttributes(),
    )


def _summary_with_count(ctx: ExtractionContext, _fired: list[str]) -> str:
    return f"found {len(ctx.candidates)}"


async def test_run_executes_each_enricher_and_dedups() -> None:
    """All enrichers run; same-name candidates are deduped after."""
    e1 = MagicMock()
    e1.enrich = AsyncMock(side_effect=lambda c: c.candidates.append(_candidate("X")))
    e2 = MagicMock()
    e2.enrich = AsyncMock(side_effect=lambda c: c.candidates.append(_candidate("X")))

    level = EnrichmentLevel(
        name="enrich", enrichers=[e1, e2], summary_fn=_summary_with_count
    )
    ctx = ExtractionContext(url=None, user_id="u1", supplementary_text="...")

    executed, summary = await level.run(ctx)

    assert executed is True
    e1.enrich.assert_awaited_once_with(ctx)
    e2.enrich.assert_awaited_once_with(ctx)
    # Both appended "X" — dedup collapses to one.
    assert len(ctx.candidates) == 1
    assert summary == "found 1"


async def test_requires_url_skips_when_url_none() -> None:
    """A URL-only level must not call enrichers when url is None."""
    e1 = MagicMock()
    e1.enrich = AsyncMock()

    level = EnrichmentLevel(
        name="deep",
        enrichers=[e1],
        summary_fn=_summary_with_count,
        requires_url=True,
    )
    ctx = ExtractionContext(url=None, user_id="u1")

    executed, summary = await level.run(ctx)

    assert executed is False
    assert summary == ""
    e1.enrich.assert_not_called()


async def test_requires_url_runs_when_url_present() -> None:
    e1 = MagicMock()
    e1.enrich = AsyncMock()

    level = EnrichmentLevel(
        name="deep",
        enrichers=[e1],
        summary_fn=_summary_with_count,
        requires_url=True,
    )
    ctx = ExtractionContext(url="https://tiktok.com/x", user_id="u1")

    executed, _ = await level.run(ctx)

    assert executed is True
    e1.enrich.assert_awaited_once_with(ctx)


async def test_summary_fn_receives_fired_enricher_class_names() -> None:
    """The summary callback gets the list of enricher class names that ran."""
    captured: list[list[str]] = []

    def capture_summary(_ctx: ExtractionContext, fired: list[str]) -> str:
        captured.append(list(fired))
        return ""

    class FooEnricher:
        async def enrich(self, _ctx: ExtractionContext) -> None: ...

    class BarEnricher:
        async def enrich(self, _ctx: ExtractionContext) -> None: ...

    level = EnrichmentLevel(
        name="x",
        enrichers=[FooEnricher(), BarEnricher()],
        summary_fn=capture_summary,
    )
    await level.run(ExtractionContext(url=None, user_id="u1"))

    assert captured == [["FooEnricher", "BarEnricher"]]
