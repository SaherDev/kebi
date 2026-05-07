"""Tests for VisionFramesEnricher — KnownPlace + early-exit on photo posts."""

from unittest.mock import AsyncMock, patch

import pytest

from kebi.core.extraction.enrichers.vision_frames import VisionFramesEnricher
from kebi.core.extraction.types import (
    ExtractionContext,
    Medium,
    Producer,
)


@pytest.fixture
def vision_extractor() -> AsyncMock:
    e = AsyncMock()
    e.extract_place_names = AsyncMock(return_value=[])
    return e


@pytest.fixture
def enricher(vision_extractor: AsyncMock) -> VisionFramesEnricher:
    return VisionFramesEnricher(vision_extractor=vision_extractor)


class TestVisionFramesEnricher:
    async def test_appends_known_place_per_extracted_name(
        self,
        enricher: VisionFramesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        vision_extractor.extract_place_names.return_value = [
            "Fuji Ramen",
            "Pizza Place",
        ]
        # Mock the synchronous _capture_frames so _run reaches
        # the vision extractor with non-empty PNG bytes.
        png_header = b"\x89PNG\r\n\x1a\n"
        iend = b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
        fake_pngs = png_header + iend + png_header + iend
        ctx = ExtractionContext(
            url="https://tiktok.com/v/abc", user_id="u1"
        )
        with patch.object(enricher, "_capture_frames", return_value=fake_pngs):
            await enricher.enrich(ctx)
        names = [k.name for k in ctx.known_places]
        assert names == ["Fuji Ramen", "Pizza Place"]
        assert all(k.producer == Producer.VISION_FRAMES for k in ctx.known_places)
        assert all(k.medium == Medium.FRAME for k in ctx.known_places)

    async def test_skips_when_photo_post(
        self,
        enricher: VisionFramesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
        )
        with patch.object(enricher, "_capture_frames") as cap:
            await enricher.enrich(ctx)
        cap.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []

    async def test_skips_when_no_url(
        self,
        enricher: VisionFramesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(url=None, user_id="u1")
        with patch.object(enricher, "_capture_frames") as cap:
            await enricher.enrich(ctx)
        cap.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []

    async def test_no_op_when_capture_returns_empty(
        self,
        enricher: VisionFramesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/abc", user_id="u1"
        )
        with patch.object(enricher, "_capture_frames", return_value=b""):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []
