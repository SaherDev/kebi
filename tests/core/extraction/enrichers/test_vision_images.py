"""Tests for VisionImagesEnricher — photo-post image vision extraction."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.vision_images import VisionImagesEnricher
from totoro_ai.core.extraction.types import ExtractionContext, Medium, Producer


@pytest.fixture
def vision_extractor() -> AsyncMock:
    extractor = AsyncMock()
    extractor.extract_place_names = AsyncMock(return_value=[])
    return extractor


@pytest.fixture
def enricher(vision_extractor: AsyncMock) -> VisionImagesEnricher:
    return VisionImagesEnricher(vision_extractor=vision_extractor)


def _http_response(content: bytes, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.content = content
    response.raise_for_status = MagicMock()
    if status >= 400:
        response.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return response


def _patched_client(side_effect: list) -> MagicMock:  # type: ignore[type-arg]
    """Build an `httpx.AsyncClient` mock that yields the given responses
    in order from `client.get(...)`."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=side_effect)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


class TestVisionImagesEnricher:
    async def test_skips_when_not_photo_post(
        self,
        enricher: VisionImagesEnricher,
        vision_extractor: AsyncMock,
    ) -> None:
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        with patch("httpx.AsyncClient") as client_cls:
            await enricher.enrich(ctx)
        client_cls.assert_not_called()
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
        with patch("httpx.AsyncClient") as client_cls:
            await enricher.enrich(ctx)
        client_cls.assert_not_called()
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
        with patch("httpx.AsyncClient") as client_cls:
            await enricher.enrich(ctx)
        client_cls.assert_not_called()
        vision_extractor.extract_place_names.assert_not_called()

    async def test_downloads_via_httpx_and_extracts_names(
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
        client = _patched_client(
            [_http_response(b"img1bytes"), _http_response(b"img2bytes")]
        )
        with patch("httpx.AsyncClient", return_value=client):
            await enricher.enrich(ctx)

        # Both URLs fetched.
        assert client.get.await_count == 2
        urls_called = [c.args[0] for c in client.get.await_args_list]
        assert urls_called == ["https://cdn/1.jpg", "https://cdn/2.jpg"]

        vision_extractor.extract_place_names.assert_awaited_once_with(
            [b"img1bytes", b"img2bytes"]
        )
        assert [k.name for k in ctx.known_places] == ["Fuji Ramen", "Pizza Place"]
        assert all(k.producer == Producer.VISION_IMAGES for k in ctx.known_places)
        assert all(k.medium == Medium.IMAGE for k in ctx.known_places)

    async def test_caps_request_count_at_max_images(
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
        client = _patched_client([_http_response(b"")] * 15)
        with patch("httpx.AsyncClient", return_value=client):
            await enricher.enrich(ctx)
        # Hard cap at 10 even when image_urls is longer.
        assert client.get.await_count == 10

    async def test_skips_failed_download_but_keeps_others(
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
        client = _patched_client(
            [_http_response(b"ok-bytes"), _http_response(b"", status=500)]
        )
        with patch("httpx.AsyncClient", return_value=client):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_awaited_once_with([b"ok-bytes"])
        assert [k.name for k in ctx.known_places] == ["Cafe X"]

    async def test_returns_silently_when_all_downloads_fail(
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
        client = _patched_client([_http_response(b"", status=500)])
        with patch("httpx.AsyncClient", return_value=client):
            await enricher.enrich(ctx)
        vision_extractor.extract_place_names.assert_not_called()
        assert ctx.known_places == []
