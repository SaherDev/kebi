"""Tests for TikTokOEmbedEnricher."""

from unittest.mock import AsyncMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.tiktok_oembed import TikTokOEmbedEnricher
from totoro_ai.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)


@pytest.fixture
def enricher() -> TikTokOEmbedEnricher:
    return TikTokOEmbedEnricher()


class TestTikTokOEmbedEnricher:
    async def test_sets_caption_from_oembed(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with patch.object(
            enricher, "_fetch_caption", new=AsyncMock(return_value="Fuji Ramen caption")
        ):
            await enricher.enrich(ctx)
        assert ctx.caption == "Fuji Ramen caption"

    async def test_appends_text_evidence_when_caption_written(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with patch.object(
            enricher, "_fetch_caption", new=AsyncMock(return_value="Fuji Ramen")
        ):
            await enricher.enrich(ctx)
        assert ctx.text_evidence == [
            Evidence(
                producer=Producer.TIKTOK_OEMBED,
                medium=Medium.CAPTION,
                snippet="Fuji Ramen",
            )
        ]

    async def test_first_write_wins_does_not_overwrite(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/123", user_id="u1", caption="existing"
        )
        with patch.object(
            enricher, "_fetch_caption", new=AsyncMock(return_value="new caption")
        ):
            await enricher.enrich(ctx)
        assert ctx.caption == "existing"
        assert ctx.text_evidence == []

    async def test_skips_when_no_url(self, enricher: TikTokOEmbedEnricher) -> None:
        ctx = ExtractionContext(url=None, user_id="u1")
        with patch.object(enricher, "_fetch_caption", new=AsyncMock()) as mock_fetch:
            await enricher.enrich(ctx)
        mock_fetch.assert_not_called()

    async def test_skips_when_host_is_not_tiktok(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://youtube.com/watch?v=123", user_id="u1")
        with patch.object(enricher, "_fetch_caption", new=AsyncMock()) as mock_fetch:
            await enricher.enrich(ctx)
        mock_fetch.assert_not_called()
        assert ctx.caption is None
        assert ctx.platform is None

    async def test_sets_platform_tiktok_on_successful_fetch(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with patch.object(
            enricher, "_fetch_caption", new=AsyncMock(return_value="some caption")
        ):
            await enricher.enrich(ctx)
        assert ctx.platform == "tiktok"

    async def test_platform_first_write_wins(
        self, enricher: TikTokOEmbedEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        ctx.platform = "instagram"
        with patch.object(
            enricher, "_fetch_caption", new=AsyncMock(return_value="caption")
        ):
            await enricher.enrich(ctx)
        assert ctx.platform == "instagram"

    async def test_propagates_http_error(self, enricher: TikTokOEmbedEnricher) -> None:
        """Exceptions must NOT be caught internally — circuit breaker handles them."""
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with (
            patch.object(
                enricher,
                "_fetch_caption",
                new=AsyncMock(side_effect=RuntimeError("timeout")),
            ),
            pytest.raises(RuntimeError, match="timeout"),
        ):
            await enricher.enrich(ctx)
