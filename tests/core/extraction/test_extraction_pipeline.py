"""Tests for ExtractionPipeline — search-first flow with v2 search service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.config import (
    ConfidenceConfig,
    ConfidenceWeights,
    ExtractionConfig,
    ExtractionThresholds,
)
from kebi.core.extraction.candidate_mapper import ResolverOutput, normalize_query
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
from kebi.core.places import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceTag,
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
        caption: str | None = None,
    ) -> None:
        self.name = name
        self.summary_fn = inline_summary
        self._seeds = seeds or []
        self._executed = executed
        self.requires_url = requires_url
        self._caption = caption

    async def run(self, context: ExtractionContext) -> tuple[bool, list[str]]:
        if self.requires_url and context.url is None:
            return False, []
        if not self._executed:
            return False, []
        if self._caption is not None:
            context.caption = self._caption
        for kp in self._seeds:
            context.known_places.append(kp)
        return True, ["StubEnricher"]


class _IdentityResolver:
    """Test resolver: identity query map over known_places, no shared
    location/tags — preserves pre-ADR-080 raw-name search behavior."""

    async def resolve(self, context: ExtractionContext) -> ResolverOutput:
        return ResolverOutput(
            queries={
                normalize_query(kp.name): kp.name.strip()
                for kp in context.known_places
                if kp.name and kp.name.strip()
            },
            location=None,
            post_tags=[],
        )


def _make_pipeline(
    levels: list[_StubLevel],
    picker_returns: list[ValidatedCandidate] | None = None,
    search_results_by_query: dict[str, list[PlaceObject]] | None = None,
    resolver: Any | None = None,
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

    @asynccontextmanager
    async def _factory() -> AsyncIterator[MagicMock]:
        yield search_service

    extraction_config = ExtractionConfig(
        confidence_weights=ConfidenceWeights(base_scores={}, places_modifiers={}),
        thresholds=ExtractionThresholds(),
        confidence=ConfidenceConfig(),
    )

    pipeline = ExtractionPipeline(
        levels=levels,  # type: ignore[arg-type]
        search_service=search_service,
        search_service_factory=_factory,
        resolver=resolver or _IdentityResolver(),
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
    out = (
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    ).candidates
    assert len(out) == 1
    # Picker called once (inline level), not twice — early exit on hit.
    assert picker.pick.await_count == 1


@pytest.mark.asyncio
async def test_no_inline_picks_runs_deep_level() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name="A", producer=Producer.GOOGLE_MAPS_LIST, medium=Medium.LIST)
        ],
    )
    deep = _StubLevel(
        name="deep",
        seeds=[
            KnownPlace(name="B", producer=Producer.VISION_FRAMES, medium=Medium.FRAME)
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
    out = (
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    ).candidates
    assert out == []
    assert picker.pick.await_count == 2


@pytest.mark.asyncio
async def test_no_url_skips_deep_level() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)],
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
async def test_resolver_cleans_queries_drops_noise_and_passes_shared_context() -> None:
    """ADR-080: search uses the resolver's cleaned query + shared
    location; resolver-dropped names are not searched; the picker
    receives the shared post-level tags."""
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(
                name="Keep Me", producer=Producer.VISION_IMAGES, medium=Medium.IMAGE
            ),
            KnownPlace(
                name="Drop Me", producer=Producer.VISION_IMAGES, medium=Medium.IMAGE
            ),
        ],
    )
    shared_tag = PlaceTag(type="atmosphere", value="upscale", source="llm")

    class _Resolver:
        async def resolve(self, context: ExtractionContext) -> ResolverOutput:
            return ResolverOutput(
                queries={normalize_query("Keep Me"): "Keep Me Cleaned"},
                location=LocationContext(city="Bangkok"),
                post_tags=[shared_tag],
            )

    pipeline, picker, search_service = _make_pipeline(
        levels=[inline],
        picker_returns=[],
        search_results_by_query={"Keep Me Cleaned": [_place_object("g:k", "Keep")]},
        resolver=_Resolver(),
    )
    await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)

    queries = [c.args[0] for c in search_service.find.call_args_list]
    searched_names = [n for q in queries for n in (q.place_names or [])]
    assert searched_names == ["Keep Me Cleaned"]  # cleaned; "Drop Me" skipped
    assert queries[0].location is not None
    assert queries[0].location.city == "Bangkok"
    # shared tags forwarded to the classifier
    assert picker.pick.await_count == 1
    assert picker.pick.call_args.kwargs["shared_tags"] == [shared_tag]


@pytest.mark.asyncio
async def test_concurrent_fanout_uses_per_task_session_no_shared_session() -> None:
    """Regression: the parallel fan-out must use the factory (one fresh
    session per query), never a single shared session.

    Simulates SQLAlchemy's non-concurrency-safe AsyncSession: each
    factory-yielded service is backed by its own session that raises
    if re-entered while already active. With a shared session (the old
    bug) the concurrent finds would collide and degrade to []; with
    per-task sessions all names resolve. The pipeline's single
    `search_service` is wired to raise, proving the fan-out does not
    use it.
    """
    names = ["Alpha", "Beta", "Gamma"]
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(name=n, producer=Producer.VISION_IMAGES, medium=Medium.IMAGE)
            for n in names
        ],
    )

    picker = MagicMock()
    captured: dict[str, Any] = {}

    async def _pick(
        context: Any, search_set: Any, shared_tags: Any = None
    ) -> list[ValidatedCandidate]:
        captured["search_set"] = dict(search_set)
        return []

    picker.pick = AsyncMock(side_effect=_pick)

    shared = MagicMock()
    shared.find = AsyncMock(
        side_effect=AssertionError("fan-out used the shared session")
    )

    class _SessionSim:
        """One independent session. Re-entering the SAME instance while
        already active (the shared-session bug) raises, like SQLAlchemy."""

        def __init__(self) -> None:
            self._active = False

        async def find(self, query: Any, limit: int = 5) -> list[PlaceObject]:
            if self._active:
                raise RuntimeError(
                    "concurrent operations are not permitted"
                )  # pragma: no cover
            self._active = True
            try:
                await asyncio.sleep(0)  # force interleave across tasks
                name = (query.place_names or [""])[0]
                return [_place_object(f"google:{name.lower()}", name)]
            finally:
                self._active = False

    @asynccontextmanager
    async def _factory() -> AsyncIterator[_SessionSim]:
        # A fresh session/service per call — the whole point of the fix.
        yield _SessionSim()

    extraction_config = ExtractionConfig(
        confidence_weights=ConfidenceWeights(base_scores={}, places_modifiers={}),
        thresholds=ExtractionThresholds(),
        confidence=ConfidenceConfig(),
    )
    pipeline = ExtractionPipeline(
        levels=[inline],  # type: ignore[list-item]
        search_service=shared,
        search_service_factory=_factory,
        resolver=_IdentityResolver(),
        picker=picker,
        extraction_config=extraction_config,
    )

    await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)

    search_set = captured["search_set"]
    assert {ar.place.place_name for ar in search_set.values()} == set(names)


@pytest.mark.asyncio
async def test_dedup_collapses_same_provider_id() -> None:
    inline = _StubLevel(
        name="inline",
        seeds=[KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)],
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
    out = (
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    ).candidates
    assert len(out) == 1


@pytest.mark.asyncio
async def test_cap_exceeded_raises_too_many_candidates() -> None:
    seeds = [
        KnownPlace(name=f"name_{i}", producer=Producer.LLM_NER, medium=Medium.CAPTION)
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
async def test_caption_only_post_extracts_via_resolver_discovery() -> None:
    """UC1: a venue named only in the caption (no list, no vision, no
    pin) is discovered by the resolver, appended as an LLM_NER
    KnownPlace, and flows through search -> pick like any other name."""
    inline = _StubLevel(name="inline", caption="Best pad thai at Thip Samai")

    class _DiscoveringResolver:
        async def resolve(self, context: ExtractionContext) -> ResolverOutput:
            context.known_places.append(
                KnownPlace(
                    name="Thip Samai",
                    producer=Producer.LLM_NER,
                    medium=Medium.CAPTION,
                )
            )
            return ResolverOutput(
                queries={normalize_query("Thip Samai"): "Thip Samai Bangkok"},
                location=None,
                post_tags=[],
            )

    pipeline, _, search_service = _make_pipeline(
        levels=[inline],
        picker_returns=[_candidate("Thip Samai", "google:thip")],
        search_results_by_query={
            "Thip Samai Bangkok": [_place_object("google:thip", "Thip Samai")]
        },
        resolver=_DiscoveringResolver(),
    )
    out = (
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    ).candidates

    assert len(out) == 1
    assert out[0].place_name == "Thip Samai"
    # The discovered name was searched via its resolver-cleaned query.
    searched = [
        n
        for c in search_service.find.call_args_list
        for n in (c.args[0].place_names or [])
    ]
    assert searched == ["Thip Samai Bangkok"]


@pytest.mark.asyncio
async def test_resolver_discovery_re_enforces_candidate_cap() -> None:
    """Names the resolver discovers in free text are appended after the
    pre-resolve cap check, so the pipeline re-enforces the limit after
    resolve() — discovery cannot blow past the candidate ceiling."""
    seeds = [
        KnownPlace(
            name=f"seed_{i}", producer=Producer.VISION_IMAGES, medium=Medium.IMAGE
        )
        for i in range(20)
    ]
    inline = _StubLevel(name="inline", seeds=seeds)

    class _DiscoveringResolver:
        async def resolve(self, context: ExtractionContext) -> ResolverOutput:
            for i in range(10):
                context.known_places.append(
                    KnownPlace(
                        name=f"found_{i}",
                        producer=Producer.LLM_NER,
                        medium=Medium.CAPTION,
                    )
                )
            return ResolverOutput(queries={}, location=None, post_tags=[])

    pipeline, _, search_service = _make_pipeline(
        levels=[inline], resolver=_DiscoveringResolver()
    )
    with pytest.raises(TooManyCandidatesError) as exc:
        await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)
    assert exc.value.found == 30
    assert exc.value.limit == _TEST_LIMIT
    # Re-check fires before the search fan-out.
    search_service.find.assert_not_called()


@pytest.mark.asyncio
async def test_geo_features_filtered_from_search_results() -> None:
    """Administrative-name results should be dropped before the picker."""
    inline = _StubLevel(
        name="inline",
        seeds=[KnownPlace(name="A", producer=Producer.LLM_NER, medium=Medium.CAPTION)],
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


@pytest.mark.asyncio
async def test_per_candidate_location_biases_each_search() -> None:
    """ADR-082: each search is biased by that candidate's own location
    when the resolver supplied one (multi-destination post), and by the
    shared post location otherwise."""
    inline = _StubLevel(
        name="inline",
        seeds=[
            KnownPlace(
                name="Inntel Hotel",
                producer=Producer.LLM_NER,
                medium=Medium.CAPTION,
            ),
            KnownPlace(
                name="Rijksmuseum",
                producer=Producer.LLM_NER,
                medium=Medium.CAPTION,
            ),
        ],
    )

    class _MultiLocationResolver:
        async def resolve(self, context: ExtractionContext) -> ResolverOutput:
            return ResolverOutput(
                queries={
                    normalize_query("Inntel Hotel"): "Inntel Hotel Zaandam",
                    normalize_query("Rijksmuseum"): "Rijksmuseum",
                },
                location=LocationContext(city="Amsterdam"),
                query_locations={
                    normalize_query("Inntel Hotel"): LocationContext(city="Zaandam"),
                },
                post_tags=[],
            )

    pipeline, _, search_service = _make_pipeline(
        levels=[inline],
        picker_returns=[],
        search_results_by_query={
            "Inntel Hotel Zaandam": [_place_object("g:i", "Inntel")],
            "Rijksmuseum": [_place_object("g:r", "Rijksmuseum")],
        },
        resolver=_MultiLocationResolver(),
    )
    await pipeline.run(url="https://x.com", user_id="u1", limit=_TEST_LIMIT)

    bias = {
        (c.args[0].place_names or [""])[0]: c.args[0].location
        for c in search_service.find.call_args_list
    }
    # Per-candidate override.
    assert bias["Inntel Hotel Zaandam"] is not None
    assert bias["Inntel Hotel Zaandam"].city == "Zaandam"
    # No override → shared post location.
    assert bias["Rijksmuseum"] is not None
    assert bias["Rijksmuseum"].city == "Amsterdam"
