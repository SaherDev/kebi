"""Tests for url_source.source_from_url.

`canonicalize_url` (which folded in the former `normalize_url`'s
TikTok photo→video rewrite) is exercised in `test_canonicalize_url.py`.
"""

import pytest

from kebi.core.extraction.url_source import source_from_url
from kebi.core.places_v2 import PlaceSource


class TestSourceFromUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.tiktok.com/v/123", PlaceSource.tiktok),
            ("https://www.instagram.com/p/abc/", PlaceSource.instagram),
            ("https://www.youtube.com/watch?v=abc", PlaceSource.youtube),
            ("https://youtu.be/abc", PlaceSource.youtube),
            ("https://maps.app.goo.gl/abc", PlaceSource.google_maps_list),
            ("https://example.com/blog/post", None),
            (None, None),
        ],
    )
    def test_source_detection(
        self, url: str | None, expected: PlaceSource | None
    ) -> None:
        assert source_from_url(url) == expected
