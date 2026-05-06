"""Tests for url_source — source detection and URL normalization."""

import pytest

from totoro_ai.core.extraction.url_source import normalize_url, source_from_url
from totoro_ai.core.places import PlaceSource


class TestSourceFromUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.tiktok.com/v/123", PlaceSource.tiktok),
            ("https://www.instagram.com/p/abc/", PlaceSource.instagram),
            ("https://www.youtube.com/watch?v=abc", PlaceSource.youtube),
            ("https://youtu.be/abc", PlaceSource.youtube),
            ("https://maps.app.goo.gl/abc", PlaceSource.google_maps),
            ("https://example.com/blog/post", None),
            (None, None),
        ],
    )
    def test_source_detection(
        self, url: str | None, expected: PlaceSource | None
    ) -> None:
        assert source_from_url(url) == expected


class TestNormalizeUrl:
    def test_tiktok_photo_path_rewritten_to_video(self) -> None:
        url = "https://www.tiktok.com/@hotspotfinders/photo/7509923486517824790"
        out = normalize_url(url)
        assert (
            out == "https://www.tiktok.com/@hotspotfinders/video/7509923486517824790"
        )

    def test_tiktok_photo_with_query_string_preserved(self) -> None:
        url = (
            "https://www.tiktok.com/@hotspotfinders/photo/7509923486517824790"
            "?is_from_webapp=1&sender_device=pc"
        )
        out = normalize_url(url)
        assert "/video/7509923486517824790" in out
        assert "is_from_webapp=1" in out

    def test_tiktok_video_path_unchanged(self) -> None:
        url = "https://www.tiktok.com/@user/video/123"
        assert normalize_url(url) == url

    def test_non_tiktok_url_unchanged(self) -> None:
        url = "https://www.instagram.com/p/abc/"
        assert normalize_url(url) == url

    def test_none_returns_none(self) -> None:
        assert normalize_url(None) is None

    def test_case_insensitive_path(self) -> None:
        url = "https://www.tiktok.com/@user/Photo/123"
        out = normalize_url(url)
        assert "/video/123" in out
