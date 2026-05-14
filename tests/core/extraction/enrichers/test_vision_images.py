"""Tests for VisionImagesEnricher — photo-post image vision extraction."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kebi.core.extraction.enrichers.vision_images import VisionImagesEnricher
from kebi.core.extraction.types import ExtractionContext, Medium, Producer


@pytest.fixture
def vision_extractor() -> AsyncMock:
    extractor = AsyncMock()
    extractor.extract_place_names = AsyncMock(return_value=[])
    return extractor


def _http_response(content: bytes, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.content = content
    response.raise_for_status = MagicMock()
    if status >= 400:
        response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return response


def _mock_http(side_effect: list) -> AsyncMock:  # type: ignore[type-arg]
    """Build an injectable `httpx.AsyncClient` mock that yields the given
    responses in order from `client.get(...)`."""
    http = AsyncMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(side_effect=side_effect)
    return http


@pytest.fixture
def enricher(vision_extractor: AsyncMock) -> VisionImagesEnricher:
    """Default enricher with an empty injected client (overridden per-test
    when the test cares about download behavior)."""
    return VisionImagesEnricher(
        vision_extractor=vision_extractor,
        http=AsyncMock(spec=httpx.AsyncClient),
    )


class TestVisionImagesEnricher:
    async def test_skips_when_not_photo_post(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        await enricher.enrich(ctx)
        http.get.assert_not_awaited()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_skips_when_no_image_urls(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
        )
        await enricher.enrich(ctx)
        http.get.assert_not_awaited()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_skips_unsupported_source(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        ctx = ExtractionContext(
            url="https://youtube.com/watch?v=x",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://yt/1.jpg"],
        )
        await enricher.enrich(ctx)
        http.get.assert_not_awaited()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_downloads_via_httpx_and_extracts_names(
        self,
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
        http = _mock_http([_http_response(b"img1bytes"), _http_response(b"img2bytes")])
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        await enricher.enrich(ctx)

        # Both URLs fetched.
        assert http.get.await_count == 2
        urls_called = [c.args[0] for c in http.get.await_args_list]
        assert urls_called == ["https://cdn/1.jpg", "https://cdn/2.jpg"]

        vision_extractor.extract_place_names.assert_awaited_once_with(
            [b"img1bytes", b"img2bytes"]
        )
        assert [k.name for k in ctx.known_places] == ["Fuji Ramen", "Pizza Place"]
        assert all(k.producer == Producer.VISION_IMAGES for k in ctx.known_places)
        assert all(k.medium == Medium.IMAGE for k in ctx.known_places)

    async def test_caps_request_count_at_max_images(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://instagram.com/p/long",
            user_id="u1",
            is_photo_post=True,
            image_urls=[f"https://cdn/{i}.jpg" for i in range(15)],
        )
        http = _mock_http([_http_response(b"")] * 15)
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        await enricher.enrich(ctx)
        # Hard cap at 10 even when image_urls is longer.
        assert http.get.await_count == 10

    async def test_skips_failed_download_but_keeps_others(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        vision_extractor.extract_place_names.return_value = ["Cafe X"]
        ctx = ExtractionContext(
            url="https://instagram.com/p/abc",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://cdn/ok.jpg", "https://cdn/bad.jpg"],
        )
        http = _mock_http(
            [_http_response(b"ok-bytes"), _http_response(b"", status=500)]
        )
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_awaited_once_with([b"ok-bytes"])
        assert [k.name for k in ctx.known_places] == ["Cafe X"]

    async def test_returns_silently_when_all_downloads_fail(
        self,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
            image_urls=["https://cdn/bad.jpg"],
        )
        http = _mock_http([_http_response(b"", status=500)])
        enricher = VisionImagesEnricher(vision_extractor=vision_extractor, http=http)
        await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []
