"""Tests for ExtractionPipeline — search-first flow with v2 search service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.config import (
    ConfidenceConfig,
    ConfidenceWeights,
    ExtractionConfig,
    ExtractionThresholds,
)
from kebi.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    TooManyCandidatesError,
    inline_summary,
)
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places_v2 import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
)

_TEST_LIMIT = 25


def _candidate(
    place_name: str = "Chez Claude",
    provider_id: str = "google:abc",
    confidence: float = 0.85,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=place_name,
        provider_id=provider_id,
        categories=[PlaceCategory.restaurant],
        tags=[],
        confidence=confidence,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )


def _place_object(
    provider_id: str = "google:abc",
    place_name: str = "Chez Claude",
    categories: list[PlaceCategory] | None = None,
) -> PlaceObject:
    return PlaceObject(
        provider_id=provider_id,
        place_name=place_name,
        categories=categories or [PlaceCategory.restaurant],
        location=LocationContext(city="Bangkok"),
    )


class _StubLevel:
    """Inert EnrichmentLevel stub.

    `seeds`: list of KnownPlace to drop on context.known_places when
             this level executes.
    `executed`: whether `run()` should report executed=True (False
             means "skipped" — e.g. requires_url with no URL).
    """

    def __init__(
        self,
        name: str = "stub",
        seeds: list[KnownPlace] | None = None,
        executed: bool = True,
        requires_url: bool = False,
    ) -> None:
        self.name = name
        self.summary_fn = inline_summary
        self._seeds = seeds or []
        self._executed = executed
        self.requires_url = requires_url

    async def run(self, context: ExtractionContext) -> tuple[bool, list[str]]:
        if self.requires_url and context.url is None:
            return False, []
        if not self._executed:
            return False, []
        for kp in self._seeds:
            context.known_places.append(kp)
        return True, ["StubEnricher"]


def _make_pipeline(
    levels: list[_StubLevel],
    picker_returns: list[ValidatedCandidate] | None = None,
    search_results_by_query: dict[str, list[PlaceObject]] | None = None,
) -> tuple[ExtractionPipeline, MagicMock, MagicMock]:
    picker = MagicMock()
    picker.pick = AsyncMock(return_value=picker_returns or [])

    search_service = MagicMock()
    results_map = search_results_by_query or {}

    async def _find(query: Any, limit: int = 5) -> list[PlaceObject]:
        names = query.place_names or []
        merged: list[PlaceObject] = []
        for name in names:
            merged.extend(results_map.get(name, []))
        return merged

    search_service.find = AsyncMock(side_effect=_find)

    extraction_config = ExtractionConfig(
        confidence_weights=ConfidenceWeights(
            base_scores={}, places_modifiers={}
        ),
        thresholds=ExtractionThresholds(),
        confidence=ConfidenceConfig(),
    )

    pipeline = ExtractionPipeline(
        levels=levels,  # type: ignore[arg-type]
        search_service=search_service,
        picker=picker,
        extraction_config=extraction_config,
    )
    return pipeline, picker, search_service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_level_picks_short_circuit_subsequent_levels() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(
                name="Chez Claude",
                producer=Producer.GOOGLE_MAPS_LIST,
                medium=Medium.LIST,
            )
        ],
    )
    deep = _StubLevel(name="deep", executed=True)
    candidate = _candidate()
    pipeline, picker, _ = _make_pipeline(
        levels=[inline, deep],
        picker_returns=[candidate],
        search_results_by_query={"Chez Claude": [_place_object()]},
    )
    out = await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert len(out) == 1
    # Picker called once (inline level), not twice — early exit on hit.
    assert picker.pick.await_count == 1


@pytest.mark.asyncio
async def test_no_inline_picks_runs_deep_level() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(
                name="A", producer=Producer.GOOGLE_MAPS_LIST, medium=Medium.LIST
            )
        ],
    )
    deep = _StubLevel(
        name="deep",
        seeds=[
            KnownPlace(
                name="B", producer=Producer.VISION_FRAMES, medium=Medium.FRAME
            )
        ],
    )
    pipeline, picker, _ = _make_pipeline(
        levels=[inline, deep],
        picker_returns=[],
        search_results_by_query={
            "A": [_place_object("google:a", "A")],
            "B": [_place_object("google:b", "B")],
        },
    )
    out = await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert out == []
    assert picker.pick.await_count == 2


@pytest.mark.asyncio
async def test_no_url_skips_deep_level() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)
        ],
    )
    deep = _StubLevel(name="deep", requires_url=True)
    pipeline, picker, _ = _make_pipeline(
        levels=[inline, deep],
        picker_returns=[],
        search_results_by_query={"A": [_place_object("google:a", "A")]},
    )
    await pipeline.run(url=None, user_id="u1", limit=_TEST_LIMIT)
    assert picker.pick.await_count == 1


@pytest.mark.asyncio
async def test_search_service_invoked_once_per_unique_name() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION),
            KnownPlace(
                name="A", producer=Producer.GOOGLE_MAPS_LIST, medium=Medium.LIST
            ),  # dup
            KnownPlace(name="B", producer=Producer.LLM_NER, medium=Medium.CAPTION),
        ],
    )
    pipeline, _, search_service = _make_pipeline(
        levels=[inline],
        picker_returns=[],
        search_results_by_query={
            "A": [_place_object("google:a", "A")],
            "B": [_place_object("google:b", "B")],
        },
    )
    await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert search_service.find.await_count == 2


@pytest.mark.asyncio
async def test_dedup_collapses_same_provider_id() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)
        ],
    )
    picks = [
        _candidate(provider_id="google:dup"),
        _candidate(provider_id="google:dup"),
    ]
    pipeline, _, _ = _make_pipeline(
        levels=[inline],
        picker_returns=picks,
        search_results_by_query={"A": [_place_object("google:dup", "A")]},
    )
    out = await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_cap_exceeded_raises_too_many_candidates() -> None:
    seeds = [
        KnownPlace(
            name=f"name_{i}", producer=Producer.LLM_NER, medium=Medium.CAPTION
        )
        for i in range(30)
    ]
    inline = _StubLevel(name="inline", seeds=seeds)
    pipeline, _, search_service = _make_pipeline(levels=[inline])
    with pytest.raises(TooManyCandidatesError) as exc:
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert exc.value.found == 30
    assert exc.value.limit == _TEST_LIMIT
    search_service.find.assert_not_called()


@pytest.mark.asyncio
async def test_geo_features_filtered_from_search_results() -> None:
    """Administrative-name results should be dropped before the picker."""
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)
        ],
    )
    real_venue = _place_object("google:venue", "Joe Pizza")
    admin_result = PlaceObject(
        provider_id="google:road1",
        place_name="Sukhumvit Road",
        categories=[],
    )
    pipeline, picker, _ = _make_pipeline(
        levels=[inline],
        picker_returns=[],
        search_results_by_query={"A": [real_venue, admin_result]},
    )
    await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    pick_args = picker.pick.await_args
    search_set = pick_args.args[1]
    assert "google:venue" in search_set
    assert "google:road1" not in search_set
