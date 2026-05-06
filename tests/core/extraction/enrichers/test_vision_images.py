"""Tests for VisionImagesEnricher — photo-post image vision extraction."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.vision_images import VisionImagesEnricher
from totoro_ai.core.extraction.types import ExtractionContext


@pytest.fixture
def vision_extractor() -> AsyncMock:
    extractor = AsyncMock()
    extractor.extract_place_names = AsyncMock(return_value=[])
    return extractor


@pytest.fixture
def enricher(vision_extractor: AsyncMock) -> VisionImagesEnricher:
    return VisionImagesEnricher(vision_extractor=vision_extractor)


def _mock_proc(stdout: bytes, returncode: int = 0) -> MagicMock:
    """Build a mock asyncio subprocess returning the given stdout."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


class TestVisionImagesEnricher:
    async def test_skips_when_not_photo_post(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_skips_when_no_image_urls(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
        )
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_skips_unsupported_source(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://youtube.com/watch?v=x",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://yt/1.jpg"],
        )
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            await enricher.enrich(ctx)
        mock_exec.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_pipes_each_image_via_ytdlp_and_extracts_names(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        vision_extractor.extract_place_names.return_value = [
            "Fuji Ramen",
            "Pizza Place",
        ]
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://cdn/1.jpg", "https://cdn/2.jpg"],
        )
        procs = [_mock_proc(b"img1bytes"), _mock_proc(b"img2bytes")]
        with patch(
            "asyncio.create_subprocess_exec", side_effect=procs
        ) as mock_exec:
            await enricher.enrich(ctx)

        # One yt-dlp invocation per image; --playlist-items is 1-indexed.
        assert mock_exec.call_count == 2
        for i, call in enumerate(mock_exec.call_args_list, start=1):
            args = call.args
            assert "--playlist-items" in args
            assert str(i) == args[args.index("--playlist-items") + 1]
            assert args[-1] == "https://tiktok.com/v/photo"

        vision_extractor.extract_place_names.assert_awaited_once()
        passed_images = vision_extractor.extract_place_names.await_args.args[0]
        assert sorted(passed_images) == sorted([b"img1bytes", b"img2bytes"])

        # Names go into known_places — LLMNEREnricher (deep-level finalizer)
        # builds the structured candidates downstream.
        assert ctx.known_places == ["Fuji Ramen", "Pizza Place"]
        assert ctx.candidates == []

    async def test_caps_subprocess_count_at_max_images(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://instagram.com/p/long",
            user_id="u1",
            is_photo_post=True,
            image_urls=[f"https://cdn/{i}.jpg" for i in range(15)],
        )
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=lambda *_a, **_kw: _mock_proc(b""),
        ) as mock_exec:
            await enricher.enrich(ctx)
        # Hard cap at 10 even when image_urls is longer.
        assert mock_exec.call_count == 10

    async def test_skips_failed_subprocess_but_keeps_others(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        vision_extractor.extract_place_names.return_value = ["Cafe X"]
        ctx = ExtractionContext(
            url="https://instagram.com/p/abc",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://cdn/ok.jpg", "https://cdn/bad.jpg"],
        )
        procs = [_mock_proc(b"ok-bytes"), _mock_proc(b"", returncode=1)]
        with patch("asyncio.create_subprocess_exec", side_effect=procs):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_awaited_once_with([b"ok-bytes"])
        assert ctx.known_places == ["Cafe X"]

    async def test_returns_silently_when_all_subprocesses_fail(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://cdn/bad.jpg"],
        )
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(b"", returncode=1),
        ):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []
        assert ctx.candidates == []
