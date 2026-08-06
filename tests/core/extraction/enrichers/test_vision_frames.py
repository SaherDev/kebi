"""Tests for VisionFramesEnricher — KnownPlace + early-exit on photo posts."""

from unittest.mock import AsyncMock, patch

import pytest

from kebi.core.extraction.enrichers.vision_frames import (
    VisionFramesEnricher,
    _build_ffmpeg_vf,
    _split_png_frames,
)
from kebi.core.extraction.types import (
    ExtractionContext,
    Medium,
    Producer,
)

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class TestSplitPngFrames:
    def test_recovers_all_frames_when_idat_contains_literal_iend(self) -> None:
        # Photographic IDAT data routinely contains the bytes "IEND" by
        # chance. The old IEND-scanning split truncated such a frame and
        # then skipped every frame after it; signature-based split must
        # return both frames byte-for-byte intact.
        frame1 = _PNG_SIG + b"AAAA" + b"IEND" + b"BBBBBBBB"
        frame2 = _PNG_SIG + b"CCCCCCCC"
        out = _split_png_frames(frame1 + frame2)
        assert out == [frame1, frame2]
        assert all(f.startswith(_PNG_SIG) for f in out)

    def test_empty_and_headerless_input(self) -> None:
        assert _split_png_frames(b"") == []
        assert _split_png_frames(b"not a png at all") == []


def test_build_ffmpeg_vf_has_no_crop_and_downscales() -> None:
    vf = _build_ffmpeg_vf(0.2)
    assert "crop" not in vf  # bottom-third crop removed (dropped top/center text)
    assert "scale=-2:640" in vf
    assert "scene\\,0.2" in vf


@pytest.fixture
def vision_extractor() -> AsyncMock:
    # Vision extractor now returns `(names, usage_dict | None)` so the
    # enricher can attach token counts to its own tracing span.
    e = AsyncMock()
    e.extract_place_names = AsyncMock(return_value=([], None))
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
        vision_extractor.extract_place_names.return_value = (
            ["Fuji Ramen", "Pizza Place"],
            {"input": 100, "output": 10, "total": 110},
        )
        # Mock the synchronous _capture_frames so _run reaches
        # the vision extractor with non-empty PNG bytes.
        png_header = b"\x89PNG\r\n\x1a\n"
        iend = b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
        fake_pngs = png_header + iend + png_header + iend
        ctx = ExtractionContext(url="https://tiktok.com/v/abc", user_id="u1")
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
        ctx = ExtractionContext(url="https://tiktok.com/v/abc", user_id="u1")
        with patch.object(enricher, "_capture_frames", return_value=b""):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []
