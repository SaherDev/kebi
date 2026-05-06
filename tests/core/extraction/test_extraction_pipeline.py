"""Tests for ExtractionPipeline (feature 019)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ExtractionLevel,
    ValidatedCandidate,
)
from totoro_ai.core.places import (
    PlaceAttributes,
    PlaceProvider,
    PlaceType,
)


def _make_validated(
    name: str = "Chez Claude",
    external_id: str = "place_abc",
    resolved_by: ExtractionLevel = ExtractionLevel.EMOJI_REGEX,
    confidence: float = 0.85,
) -> ValidatedCandidate:
    return ValidatedCandidate(
        place_name=name,
        place_type=PlaceType.food_and_drink,
        provider=PlaceProvider.google,
        external_id=external_id,
        confidence=confidence,
        resolved_by=resolved_by,
        attributes=PlaceAttributes(),
        corroborated=False,
    )


_TEST_LIMIT = 25  # default cap used by tests that don't care about the limit


def _make_pipeline(
    inline_validator_returns=None,  # type: ignore[no-untyped-def]
    background_validator_returns=None,
    background_enrichers=None,
    enrichment_seeds_candidates=0,
    finalizer=None,
) -> tuple:
    """Returns (pipeline, inline_level, validator_mock, bg_enrichers).

    `enrichment_seeds_candidates`: when non-zero, the inline level's
    enricher appends that many `CandidatePlace`s to context.candidates
    so the cap check has something real to count. Tests pass the cap
    value via `pipeline.run(..., limit=N)` directly — the pipeline
    requires `limit` and no longer reads it from config.
    `finalizer`: optional pipeline-level enricher that runs after every
    executed level (in production this is `LLMNEREnricher`).
    """
    from totoro_ai.core.config import (
        ConfidenceWeights,
        ExtractionConfig,
        ExtractionThresholds,
    )
    from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
    from totoro_ai.core.extraction.extraction_pipeline import (
        ExtractionPipeline,
        deep_summary,
        inline_summary,
    )

    seeder = MagicMock()

    async def _seed(ctx) -> None:  # type: ignore[no-untyped-def]
        for i in range(enrichment_seeds_candidates):
            ctx.candidates.append(
                CandidatePlace(
                    place_name=f"Place {i}",
                    place_type=PlaceType.food_and_drink,
                    source=ExtractionLevel.LLM_NER,
                    attributes=PlaceAttributes(),
                )
            )

    seeder.enrich = AsyncMock(side_effect=_seed)

    inline_level = EnrichmentLevel(
        name="enrich",
        enrichers=[seeder],
        summary_fn=inline_summary,
    )

    # validator returns inline_validator_returns on first call,
    # background_validator_returns on second (deep level re-validation)
    if background_validator_returns is not None:
        validator = MagicMock()
        validator.validate = AsyncMock(
            side_effect=[inline_validator_returns, background_validator_returns]
        )
    else:
        validator = MagicMock()
        validator.validate = AsyncMock(return_value=inline_validator_returns)

    if background_enrichers is None:
        background_enrichers = []

    deep_level = EnrichmentLevel(
        name="deep_enrichment",
        enrichers=background_enrichers,
        summary_fn=deep_summary,
        requires_url=True,
    )

    weights = ConfidenceWeights(
        base_scores={"CAPTION": 0.7},
        places_modifiers={"EXACT": 0.2},
    )
    extraction_config = ExtractionConfig(
        confidence_weights=weights,
        thresholds=ExtractionThresholds(),
    )

    pipeline = ExtractionPipeline(
        levels=[inline_level, deep_level],
        validator=validator,
        extraction_config=extraction_config,
        finalizer=finalizer,
    )
    return pipeline, inline_level, validator, background_enrichers


async def test_inline_candidates_found_returns_results() -> None:
    results = [_make_validated()]
    pipeline, _, _, _ = _make_pipeline(inline_validator_returns=results)

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == results


async def test_no_inline_candidates_no_bg_enrichers_returns_empty() -> None:
    pipeline, _, _, _ = _make_pipeline(inline_validator_returns=None)

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == []


async def test_no_inline_candidates_bg_enrichers_run_inline() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()
    bg_results = [_make_validated()]

    pipeline, _, _, _ = _make_pipeline(
        inline_validator_returns=None,
        background_validator_returns=bg_results,
        background_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    bg_enricher.enrich.assert_awaited_once()
    assert output == bg_results


async def test_bg_enrichers_find_nothing_returns_empty() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    pipeline, _, _, _ = _make_pipeline(
        inline_validator_returns=None,
        background_validator_returns=None,
        background_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url="https://tiktok.com/1", user_id="u1", limit=_TEST_LIMIT
    )

    assert output == []


async def test_plain_text_no_url_skips_bg_enrichers() -> None:
    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    pipeline, _, _, _ = _make_pipeline(
        inline_validator_returns=None,
        background_enrichers=[bg_enricher],
    )

    output = await pipeline.run(
        url=None, user_id="u1", supplementary_text="Some place", limit=_TEST_LIMIT
    )

    bg_enricher.enrich.assert_not_called()
    assert output == []


async def test_same_provider_id_deduped_after_validation() -> None:
    """Two validated candidates resolving to the same provider_id are
    collapsed into one with the corroboration bonus applied."""
    emoji = _make_validated(
        name="RAMEN KAISUGI Bangkok",
        external_id="ChIJrUYs1Xuf4jARDnd40CFUUAE",
        resolved_by=ExtractionLevel.EMOJI_REGEX,
        confidence=0.76,
    )
    ner = _make_validated(
        name="RAMEN KAISUGI",
        external_id="ChIJrUYs1Xuf4jARDnd40CFUUAE",
        resolved_by=ExtractionLevel.LLM_NER,
        confidence=0.64,
    )
    pipeline, _, _, _ = _make_pipeline(inline_validator_returns=[emoji, ner])

    output = await pipeline.run(
        url=None,
        user_id="u1",
        supplementary_text="RAMEN KAISUGI Bangkok",
        limit=_TEST_LIMIT,
    )

    assert isinstance(output, list)
    assert len(output) == 1
    assert output[0].resolved_by == ExtractionLevel.EMOJI_REGEX
    assert output[0].corroborated is True


async def test_plain_text_input_url_none_passes_through() -> None:
    results = [_make_validated()]
    pipeline, inline_level, _, _ = _make_pipeline(inline_validator_returns=results)

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


async def test_validator_receives_only_candidates() -> None:
    """validator.validate(candidates) — no user_id positional arg."""
    pipeline, _, validator, _ = _make_pipeline(inline_validator_returns=None)

    await pipeline.run(url="https://tiktok.com/1", user_id="u-xyz", limit=_TEST_LIMIT)

    assert validator.validate.await_count >= 1
    args = validator.validate.call_args.args
    kwargs = validator.validate.call_args.kwargs
    assert len(args) + len(kwargs) == 1


async def test_pipeline_finalizer_runs_after_each_executed_level() -> None:
    """The pipeline-level finalizer (typically NER) runs after every
    executed level — both inline and deep when both fire — and never
    when a level was skipped via requires_url."""
    finalizer = MagicMock()
    finalizer.enrich = AsyncMock()

    bg_enricher = MagicMock()
    bg_enricher.enrich = AsyncMock()

    # Inline returns nothing → deep level fires → both should call finalizer.
    pipeline, _, _, _ = _make_pipeline(
        inline_validator_returns=None,
        background_validator_returns=None,
        background_enrichers=[bg_enricher],
        finalizer=finalizer,
    )

    await pipeline.run(url="https://tiktok.com/x", user_id="u1", limit=_TEST_LIMIT)

    assert finalizer.enrich.await_count == 2


async def test_pipeline_finalizer_skipped_when_level_skipped() -> None:
    """A requires_url level on a text-only input is skipped — and the
    finalizer must not run for that skipped level."""
    finalizer = MagicMock()
    finalizer.enrich = AsyncMock()

    pipeline, _, _, _ = _make_pipeline(
        inline_validator_returns=None,
        background_enrichers=[MagicMock(enrich=AsyncMock())],
        finalizer=finalizer,
    )

    # url=None → deep level is skipped, only inline fires the finalizer.
    await pipeline.run(
        url=None, user_id="u1", supplementary_text="something", limit=_TEST_LIMIT
    )

    assert finalizer.enrich.await_count == 1


async def test_too_many_candidates_drops_request_before_validation() -> None:
    """When Phase 1 produces more candidates than `limit`, the pipeline
    raises `TooManyCandidatesError` and never calls the validator."""
    from totoro_ai.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, validator, _ = _make_pipeline(enrichment_seeds_candidates=30)

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(
            url=None, user_id="u1", supplementary_text="...", limit=25
        )

    assert exc_info.value.found == 30
    assert exc_info.value.limit == 25
    validator.validate.assert_not_called()


async def test_candidates_at_limit_proceed_normally() -> None:
    """Exactly `limit` candidates is allowed — no exception."""
    pipeline, _, validator, _ = _make_pipeline(
        inline_validator_returns=None,
        enrichment_seeds_candidates=25,
    )

    await pipeline.run(
        url=None, user_id="u1", supplementary_text="...", limit=25
    )

    validator.validate.assert_awaited()


async def test_phase3_too_many_candidates_drops_request() -> None:
    """Phase 3 background enrichers can push the candidate count back
    over the cap. The pipeline must enforce the same hard drop before
    the Phase 3 re-validation."""
    from totoro_ai.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    # Phase 1 returns nothing (validator returns None) so Phase 3 fires.
    # The bg enricher then balloons the candidate set past the cap.
    bg_enricher = MagicMock()

    async def _balloon(ctx) -> None:  # type: ignore[no-untyped-def]
        for i in range(30):
            ctx.candidates.append(
                CandidatePlace(
                    place_name=f"BG Place {i}",
                    place_type=PlaceType.food_and_drink,
                    source=ExtractionLevel.WHISPER_AUDIO,
                    attributes=PlaceAttributes(),
                )
            )

    bg_enricher.enrich = AsyncMock(side_effect=_balloon)

    pipeline, _, validator, _ = _make_pipeline(
        inline_validator_returns=None,
        background_enrichers=[bg_enricher],
    )

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(url="https://tiktok.com/1", user_id="u1", limit=25)

    assert exc_info.value.found == 30
    # Validator was called once (Phase 2 with 0 candidates) but never
    # again — Phase 3 raised before re-validation.
    assert validator.validate.await_count == 1


async def test_tight_limit_drops_request() -> None:
    """A tight per-call limit trips even with relatively few candidates."""
    from totoro_ai.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, validator, _ = _make_pipeline(enrichment_seeds_candidates=12)

    with pytest.raises(TooManyCandidatesError) as exc_info:
        await pipeline.run(url=None, user_id="u1", limit=10)

    assert exc_info.value.found == 12
    assert exc_info.value.limit == 10
    validator.validate.assert_not_called()


async def test_loose_limit_allows_many_candidates() -> None:
    """A high per-call limit lets the pipeline through with a big set."""
    pipeline, _, validator, _ = _make_pipeline(
        inline_validator_returns=None,
        enrichment_seeds_candidates=40,
    )

    # No exception — caller raised the ceiling to 50.
    await pipeline.run(url=None, user_id="u1", limit=50)
    validator.validate.assert_awaited()


async def test_too_many_candidates_emits_cap_exceeded_step() -> None:
    """The pipeline emits a `save.cap_exceeded` reasoning step before raising."""
    from totoro_ai.core.extraction.extraction_pipeline import (
        TooManyCandidatesError,
    )

    pipeline, _, _, _ = _make_pipeline(enrichment_seeds_candidates=30)

    emitted: list[tuple[str, str]] = []

    def spy(step: str, summary: str, duration_ms: float | None = None) -> None:
        emitted.append((step, summary))

    with pytest.raises(TooManyCandidatesError):
        await pipeline.run(
            url=None, user_id="u1", supplementary_text="...", emit=spy, limit=25
        )

    steps = [s for s, _ in emitted]
    assert "save.cap_exceeded" in steps
    cap_msg = next(msg for s, msg in emitted if s == "save.cap_exceeded")
    assert "30" in cap_msg
    assert "25" in cap_msg
