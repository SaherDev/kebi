"""Tests for VideoMetadataEnricher — metadata field population."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.video_metadata import VideoMetadataEnricher
from totoro_ai.core.extraction.types import (
    ExtractionContext,
    Medium,
    Producer,
)


def _mock_proc(data: dict) -> MagicMock:  # type: ignore[type-arg]
    """Build a mock subprocess result returning the given dict as JSON stdout."""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(json.dumps(data).encode(), b""))
    return proc


@pytest.fixture
def enricher() -> VideoMetadataEnricher:
    return VideoMetadataEnricher()


class TestVideoMetadataEnricher:
    async def test_populates_caption_from_description(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc({"description": "Best ramen in Bangkok", "title": None})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.caption == "Best ramen in Bangkok"

    async def test_populates_title(self, enricher: VideoMetadataEnricher) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc({"description": "caption text", "title": "My Food Video"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.title == "My Food Video"

    async def test_populates_hashtags_from_tags(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc(
            {"description": "caption", "tags": ["bangkokfood", "ramen", "fyp"]}
        )
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.hashtags == ["bangkokfood", "ramen", "fyp"]

    async def test_populates_platform_from_extractor(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc({"description": "caption", "extractor": "TikTok"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.platform == "TikTok"

    async def test_populates_location_tag(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc({"description": "caption", "location": "Bangkok, Thailand"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.location_tag == "Bangkok, Thailand"

    async def test_platform_defaults_to_unknown_when_extractor_absent(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://youtube.com/watch?v=123", user_id="u1")
        proc = _mock_proc({"description": "caption"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.platform == "unknown"

    async def test_skips_when_host_not_supported(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://example.com/v/123", user_id="u1")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        assert ctx.caption is None
        assert ctx.platform is None

    async def test_first_write_wins_caption(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/123", user_id="u1", caption="existing"
        )
        proc = _mock_proc({"description": "new description"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.caption == "existing"

    async def test_first_write_wins_title(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        ctx.title = "already set"
        proc = _mock_proc({"description": "caption", "title": "new title"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.title == "already set"

    async def test_first_write_wins_platform(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        ctx.platform = "tiktok"
        proc = _mock_proc({"description": "caption", "extractor": "Instagram"})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.platform == "tiktok"

    async def test_first_write_wins_hashtags(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        ctx.hashtags = ["existing"]
        proc = _mock_proc({"description": "caption", "tags": ["new"]})
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)
        assert ctx.hashtags == ["existing"]

    async def test_skips_when_no_url(self, enricher: VideoMetadataEnricher) -> None:
        ctx = ExtractionContext(url=None, user_id="u1")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()

    async def test_skips_when_caption_already_set(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/123", user_id="u1", caption="already"
        )
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        assert ctx.text_evidence == []

    async def test_appends_text_evidence_per_field_written(
        self, enricher: VideoMetadataEnricher
    ) -> None:
        """One Evidence entry per field yt-dlp populated: caption, title,
        each hashtag, location_tag."""
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        proc = _mock_proc(
            {
                "description": "Loved Fuji Ramen",
                "title": "Best Bangkok bites",
                "tags": ["bangkokfood", "ramen"],
                "location": "Bangkok, Thailand",
                "extractor": "TikTok",
            }
        )
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await enricher.enrich(ctx)

        producers = {e.producer for e in ctx.text_evidence}
        assert producers == {Producer.VIDEO_METADATA}
        media = [e.medium for e in ctx.text_evidence]
        # one each for caption + title + location_tag, plus one per hashtag.
        assert media.count(Medium.CAPTION) == 1
        assert media.count(Medium.TITLE) == 1
        assert media.count(Medium.LOCATION_TAG) == 1
        assert media.count(Medium.HASHTAG) == 2
