"""Tests for ExtractionPipeline (search-first flow)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.extraction.types import (
    Evidence,
    KnownPlace,
    Medium,
    Producer,
    ValidatedCandidate,
)
from kebi.core.places import (
    PlaceAttributes,
    PlaceProvider,
    PlaceType,
)


def _make_validated(
    name: str = "Chez Claude",
    external_id: str = "place_abc",
    confidence: float = 0.85,
    evidence: list[Evidence] | None = None,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.google,
        external_id=external_id,
        confidence=confidence,
        evidence=evidence or [Evidence(Producer.LLM_NER, Medium.CAPTION)],
        attributes=PlaceAttributes(),
    )


_TEST_LIMIT = 25  # default cap used by tests that don't care about the limit


def _make_pipeline(  # type: ignore[no-untyped-def]
    inline_picks=None,
    deep_picks=None,
    deep_enrichers=None,
    inline_seeds_known_places=0,
    deep_seeds_known_places=0,
):
    """Returns (pipeline, inline_level, searcher_mock, picker_mock, deep_enrichers).

    `inline_picks`: list[ValidatedCandidate] | None — what the picker
        returns after the inline level fires.
    `deep_picks`: list[ValidatedCandidate] | None — what the picker
        returns after the deep level fires (when inline_picks was empty).
    `deep_enrichers`: enrichers wired into the deep (URL-only) level.
    `inline_seeds_known_places`: when non-zero, the inline level's
        enricher appends that many `KnownPlace`s — used to drive the
        pre-search cap check.
    `deep_seeds_known_places`: same for the deep level.
    """
    from kebi.core.config import (
        ConfidenceConfig,
        ConfidenceWeights,
        ExtractionConfig,
        ExtractionThresholds,
    )
    from kebi.core.extraction.enrichment_level import EnrichmentLevel
    from kebi.core.extraction.extraction_pipeline import (
        ExtractionPipeline,
        deep_summary,
        inline_summary,
    )

    inline_enricher = MagicMock()

    async def _seed_inline(ctx) -> None:  # type: ignore[no-untyped-def]
        for i in range(inline_seeds_known_places):
            ctx.known_places.append(
                KnownPlace(
                    name=f"Place {i}",
                    producer=Producer.GOOGLE_MAPS_LIST,
                    medium=Medium.LIST,
                )
            )

    inline_enricher.enrich = AsyncMock(side_effect=_seed_inline)

    inline_level = EnrichmentLevel(
        name="enrich",
        enrichers=[inline_enricher],
        summary_fn=inline_summary,
    )

    if deep_enrichers is None:
        deep_enrichers = []

    if deep_seeds_known_places:
        seeder = MagicMock()

        async def _seed_deep(ctx) -> None:  # type: ignore[no-untyped-def]
            for i in range(deep_seeds_known_places):
                ctx.known_places.append(
                    KnownPlace(
                        name=f"Deep Place {i}",
                        producer=Producer.VISION_FRAMES,
                        medium=Medium.FRAME,
                    )
                )

        seeder.enrich = AsyncMock(side_effect=_seed_deep)
        deep_enrichers = [*deep_enrichers, seeder]

    deep_level = EnrichmentLevel(
        name="deep_enrichment",
        enrichers=deep_enrichers,
        summary_fn=deep_summary,
        requires_url=True,
    )

    searcher = MagicMock()
    searcher.search = AsyncMock()

    picker = MagicMock()
    if deep_picks is not None:
        picker.pick = AsyncMock(side_effect=[inline_picks or [], deep_picks])
    else:
        picker.pick = AsyncMock(return_value=inline_picks or [])

    weights = ConfidenceWeights(
        base_scores={"CAPTION": 0.7},
        places_modifiers={"EXACT": 0.2},
    )
    extraction_config = ExtractionConfig(
        confidence_weights=weights,
        confidence=ConfidenceConfig(),
        thresholds=ExtractionThresholds(),
    )

    pipeline = ExtractionPipeline(
        levels=[inline_level, deep_level],
        searcher=searcher,
        picker=picker,
        extraction_config=extraction_config,
    )
    return pipeline, inline_level, searcher, picker, deep_enrichers


async def test_inline_picks_returns_results() -> None:
    results = [_make_validated()]
    pipeline, _, _, _, _ = _make_pipeline(inline_picks=results)

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == results


async def test_no_inline_picks_no_deep_enrichers_returns_empty() -> None:
    pipeline, _, _, _, _ = _make_pipeline(inline_picks=None)

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == []


async def test_no_inline_picks_deep_enrichers_run_and_picker_re_runs() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()
    deep_results = [_make_validated()]

    pipeline, _, _, picker, _ = _make_pipeline(
        inline_picks=None,
        deep_picks=deep_results,
        deep_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    bg_enricher.enrich.assert_awaited_once()
    assert picker.pick.await_count == 2
    assert output == deep_results


async def test_deep_enrichers_find_nothing_returns_empty() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    pipeline, _, _, _, _ = _make_pipeline(
        inline_picks=None,
        deep_picks=None,
        deep_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == []


async def test_plain_text_no_url_skips_deep_enrichers() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    pipeline, _, _, _, _ = _make_pipeline(
        inline_picks=None,
        deep_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url=None,
        user_id="u1",
        supplementary_text="Some place",
        limit=_TEST_LIMIT,
    )

    bg_enricher.enrich.assert_not_called()
    assert output == []


async def test_same_provider_id_deduped_after_picking() -> None:
    """Two picks resolving to the same provider_id are collapsed into
    one with merged evidence and the corroboration bonus."""
    a = _make_validated(
        name="RAMEN KAISUGI Bangkok",
        external_id="ChIJrUYs1Xuf4jARDnd40CFUUAE",
        confidence=0.76,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
    )
    b = _make_validated(
        name="RAMEN KAISUGI",
        external_id="ChIJrUYs1Xuf4jARDnd40CFUUAE",
        confidence=0.64,
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)],
    )
    pipeline, _, _, _, _ = _make_pipeline(inline_picks=[a, b])

    output = await pipeline.run(
        url=None,
        user_id="u1",
        supplementary_text="RAMEN KAISUGI Bangkok",
        limit=_TEST_LIMIT,
    )

    assert isinstance(output, list)
    assert len(output) == 1
    producers = {e.producer for e in output[0].evidence}
    assert producers == {Producer.LLM_NER, Producer.VISION_FRAMES}


async def test_plain_text_input_url_none_passes_through() -> None:
    results = [_make_validated()]
    pipeline, inline_level, _, _, _ = _make_pipeline(inline_picks=results)

    output = await pipeline.run(
        url=None,
        user_id="u1",
        supplementary_text="Ramen House Paris",
        limit=_TEST_LIMIT,
    )

    assert output == results
    seeder = inline_level.enrichers[0]
    seeder.enrich.assert_awaited_once()
    ctx = seeder.enrich.call_args.args[0]
    assert ctx.url is None
    assert ctx.supplementary_text == "Ramen House Paris"


async def test_searcher_runs_on_each_executed_level() -> None:
    """Search runs after every executed level. Inline-only when deep skipped."""
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    pipeline, _, searcher, _, _ = _make_pipeline(
        inline_picks=None,
        deep_picks=None,
        deep_enrichers=[bg_enricher],
    )

    await pipeline.run(url="https://tiktok.com/x", user_id="u1", limit=_TEST_LIMIT)

    assert searcher.search.await_count == 2


async def test_searcher_skipped_when_level_skipped() -> None:
    """A requires_url level on a text-only input is skipped — search
    must not run for that skipped level."""
    pipeline, _, searcher, _, _ = _make_pipeline(
        inline_picks=None,
        deep_enrichers=[MagicMock(enrich=AsyncMock())],
    )

    await pipeline.run(
        url=None, user_id="u1", supplementary_text="something", limit=_TEST_LIMIT
    )

    # url=None → deep level skipped; only inline runs the searcher.
    assert searcher.search.await_count == 1


async def test_searcher_receives_context() -> None:
    """searcher.search(context) is called with the shared ExtractionContext."""
    pipeline, _, searcher, _, _ = _make_pipeline(inline_picks=None)

    await pipeline.run(
        url="https://tiktok.com/1", user_id="u-xyz", limit=_TEST_LIMIT
    )

    args = searcher.search.call_args.args
    kwargs = searcher.search.call_args.kwargs
    assert len(args) + len(kwargs) == 1
    ctx = args[0] if args else kwargs.get("context")
    assert ctx.user_id == "u-xyz"


async def test_too_many_known_places_drops_request_before_search() -> None:
    """When producers contributed more known_places than `limit`, the
    pipeline raises before any Google Places call."""
    from kebi.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, searcher, picker, _ = _make_pipeline(
        inline_seeds_known_places=30
    )

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(
            url=None, user_id="u1", supplementary_text="...", limit=25
        )

    assert exc_info.value.found == 30
    assert exc_info.value.limit == 25
    searcher.search.assert_not_called()
    picker.pick.assert_not_called()


async def test_known_places_at_limit_proceed_normally() -> None:
    """Exactly `limit` known_places is allowed — no exception."""
    pipeline, _, searcher, _, _ = _make_pipeline(
        inline_picks=None, inline_seeds_known_places=25
    )

    await pipeline.run(
        url=None, user_id="u1", supplementary_text="...", limit=25
    )

    searcher.search.assert_awaited()


async def test_deep_known_places_trip_cap() -> None:
    """The deep level can balloon known_places past the cap; the
    pipeline must enforce the cap before that level's Search call."""
    from kebi.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, searcher, _, _ = _make_pipeline(
        inline_picks=None,
        deep_seeds_known_places=30,
    )

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(url="https://tiktok.com/1", user_id="u1", limit=25)

    assert exc_info.value.found == 30
    # Inline searched (with 0 known_places) but deep's search must not fire.
    assert searcher.search.await_count == 1


async def test_tight_limit_drops_request() -> None:
    """A tight per-call limit trips even with relatively few known_places."""
    from kebi.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, searcher, _, _ = _make_pipeline(inline_seeds_known_places=12)

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(url=None, user_id="u1", limit=10)

    assert exc_info.value.found == 12
    assert exc_info.value.limit == 10
    searcher.search.assert_not_called()


async def test_loose_limit_allows_many_known_places() -> None:
    """A high per-call limit lets the pipeline through with a big set."""
    pipeline, _, searcher, _, _ = _make_pipeline(
        inline_picks=None, inline_seeds_known_places=40
    )

    await pipeline.run(url=None, user_id="u1", limit=50)
    searcher.search.assert_awaited()


async def test_too_many_known_places_emits_cap_exceeded_step() -> None:
    """The pipeline emits a `save.cap_exceeded` reasoning step before raising."""
    from kebi.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, _, _, _ = _make_pipeline(inline_seeds_known_places=30)

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    with pytest.raises(TooManyCandidatesError):
        await pipeline.run(
            url=None,
            user_id="u1",
            supplementary_text="...",
            emit=spy,
            limit=25,
        )

    steps = [s for s, _ in emitted]
    assert "save.cap_exceeded" in steps
    cap_msg = next(msg for s, msg in emitted if s == "save.cap_exceeded")
    assert "30" in cap_msg
    assert "25" in cap_msg
