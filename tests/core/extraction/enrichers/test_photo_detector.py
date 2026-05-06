"""Tests for PhotoDetectorEnricher — photo-post detection + image URL capture."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.photo_detector import (
    PhotoDetectorEnricher,
)
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
def enricher() -> PhotoDetectorEnricher:
    return PhotoDetectorEnricher()


class TestPhotoDetectorEnricher:
    async def test_detects_tiktok_photo_mode_playlist(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/photo123", user_id="u1")
        data = {
            "_type": "playlist",
            "entries": [
                {"vcodec": "none", "url": "https://cdn.tiktok.com/img1.jpg"},
                {"vcodec": "none", "url": "https://cdn.tiktok.com/img2.jpg"},
                {"vcodec": "none", "url": "https://cdn.tiktok.com/img3.jpg"},
            ],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert ctx.is_photo_post is True
        assert ctx.image_urls == [
            "https://cdn.tiktok.com/img1.jpg",
            "https://cdn.tiktok.com/img2.jpg",
            "https://cdn.tiktok.com/img3.jpg",
        ]

    async def test_detects_instagram_carousel_via_thumbnails(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://instagram.com/p/abc", user_id="u1")
        data = {
            "_type": "playlist",
            "entries": [
                {
                    "thumbnails": [
                        {"url": "https://ig.com/sm1.jpg", "width": 320, "height": 320},
                        {
                            "url": "https://ig.com/lg1.jpg",
                            "width": 1080,
                            "height": 1080,
                        },
                    ],
                },
                {
                    "thumbnails": [
                        {
                            "url": "https://ig.com/lg2.jpg",
                            "width": 1080,
                            "height": 1080,
                        },
                    ],
                },
            ],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert ctx.is_photo_post is True
        # Highest-resolution thumbnail wins per entry
        assert ctx.image_urls == [
            "https://ig.com/lg1.jpg",
            "https://ig.com/lg2.jpg",
        ]

    async def test_leaves_video_post_untouched(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/video123", user_id="u1")
        data = {
            "extractor": "TikTok",
            "vcodec": "h264",
            "formats": [{"vcodec": "h264", "url": "https://cdn.tiktok.com/v.mp4"}],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert ctx.is_photo_post is False
        assert ctx.image_urls == []

    async def test_caps_image_urls_at_ten(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/long", user_id="u1")
        data = {
            "_type": "playlist",
            "entries": [
                {"vcodec": "none", "url": f"https://cdn/{i}.jpg"} for i in range(15)
            ],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert ctx.is_photo_post is True
        assert len(ctx.image_urls) == 10
        assert ctx.image_urls[0] == "https://cdn/0.jpg"
        assert ctx.image_urls[-1] == "https://cdn/9.jpg"

    async def test_skips_when_already_detected(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://existing/1.jpg"],
        )
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        assert ctx.image_urls == ["https://existing/1.jpg"]

    async def test_skips_unsupported_source(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://youtube.com/watch?v=123", user_id="u1")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        assert ctx.is_photo_post is False
        assert ctx.image_urls == []

    async def test_handles_playlist_with_zero_image_entries(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/x", user_id="u1")
        data = {
            "_type": "playlist",
            "entries": [
                {"vcodec": "h264", "url": "https://cdn/v.mp4"},
            ],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert ctx.is_photo_post is False
        assert ctx.image_urls == []
        assert ctx.text_evidence == []

    async def test_appends_text_evidence_when_photo_post_detected(
        self, enricher: PhotoDetectorEnricher
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/photo123", user_id="u1")
        data = {
            "_type": "playlist",
            "entries": [
                {"vcodec": "none", "url": "https://cdn.tiktok.com/img1.jpg"},
                {"vcodec": "none", "url": "https://cdn.tiktok.com/img2.jpg"},
            ],
        }
        with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(data)):
            await enricher.enrich(ctx)
        assert len(ctx.text_evidence) == 1
        ev = ctx.text_evidence[0]
        assert ev.producer == Producer.PHOTO_DETECTOR
        assert ev.medium == Medium.IMAGE
        assert ev.snippet is None
        assert dict(ev.metadata)["image_count"] == 2
