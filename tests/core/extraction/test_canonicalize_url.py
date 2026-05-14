"""Tests for canonicalize_url — stable identity key for cache lookup (ADR-074)."""

from __future__ import annotations

import pytest

from kebi.core.extraction.url_source import canonicalize_url


class TestKnownHostStripping:
    def test_two_shares_of_same_tiktok_collapse_to_same_canonical(self) -> None:
        """Two share URLs differing only by `web_id` produce the same key.

        This is the user-reported case that motivated the canonicalizer.
        Without this collapsing, the result cache hit rate is ~0% on
        viral content (every share generates a new tracking value).

        Note: the photo→video rewrite folded in from the former
        `normalize_url` means TikTok `/photo/` paths come out as
        `/video/` paths.
        """
        a = (
            "https://www.tiktok.com/@explorewithmairy/photo/7491715293908126998"
            "?is_from_webapp=1&sender_device=pc&web_id=759538676711877992"
        )
        b = (
            "https://www.tiktok.com/@explorewithmairy/photo/7491715293908126998"
            "?is_from_webapp=1&sender_device=pc&web_id=93893"
        )
        assert canonicalize_url(a) == canonicalize_url(b)
        assert canonicalize_url(a) == (
            "https://www.tiktok.com/@explorewithmairy/video/7491715293908126998"
        )

    def test_uppercase_host_is_lowercased(self) -> None:
        assert canonicalize_url("https://WWW.TikTok.com/@u/video/123?x=1") == (
            "https://www.tiktok.com/@u/video/123"
        )

    def test_fragment_is_stripped(self) -> None:
        assert canonicalize_url("https://tiktok.com/@u/video/123#anchor") == (
            "https://tiktok.com/@u/video/123"
        )

    def test_trailing_slash_is_stripped(self) -> None:
        assert canonicalize_url("https://tiktok.com/@u/video/123/?x=1") == (
            "https://tiktok.com/@u/video/123"
        )

    def test_mobile_subdomain_recognized(self) -> None:
        assert canonicalize_url("https://m.tiktok.com/@u/video/123?web_id=x") == (
            "https://m.tiktok.com/@u/video/123"
        )

    def test_vm_short_subdomain_recognized(self) -> None:
        # vm.tiktok.com paths can still be stripped (shortlink expansion
        # is a follow-up; the path alone is already a usable cache key
        # within a single share's session).
        assert canonicalize_url("https://vm.tiktok.com/ZGabc123/?_t=q&_r=1") == (
            "https://vm.tiktok.com/ZGabc123"
        )

    def test_instagram_post_query_stripped(self) -> None:
        assert (
            canonicalize_url(
                "https://www.instagram.com/p/abc/?igshid=xyz&utm_source=share"
            )
            == "https://www.instagram.com/p/abc"
        )

    def test_youtube_watch_query_stripped(self) -> None:
        # NOTE: youtube.com's `?v=` is load-bearing — the v1 canonicalizer
        # strips it. Per-platform path normalization that preserves the
        # video id (or restructures into youtu.be/{id}) is a follow-up.
        # Today: youtube.com/watch?v=X collapses to youtube.com/watch.
        # Accept this as a known v1 limitation; trendy traffic is TikTok.
        assert canonicalize_url("https://www.youtube.com/watch?v=abc123") == (
            "https://www.youtube.com/watch"
        )


class TestTikTokPhotoRewrite:
    """Folded-in TikTok photo→video rewrite (formerly `normalize_url`).

    yt-dlp rejects TikTok `/photo/<id>` URLs; the canonical form rewrites
    them to `/video/<id>` so downstream enrichers hit a yt-dlp-readable
    URL. Applies for any URL containing `tiktok.com`, even subdomains
    that aren't in the canonical-host set.
    """

    def test_tiktok_photo_path_rewritten_to_video(self) -> None:
        url = "https://www.tiktok.com/@hotspotfinders/photo/7509923486517824790"
        assert canonicalize_url(url) == (
            "https://www.tiktok.com/@hotspotfinders/video/7509923486517824790"
        )

    def test_tiktok_photo_with_query_string_rewritten_and_stripped(self) -> None:
        url = (
            "https://www.tiktok.com/@hotspotfinders/photo/7509923486517824790"
            "?is_from_webapp=1&sender_device=pc"
        )
        # Photo path rewritten AND query stripped — one canonical form.
        assert canonicalize_url(url) == (
            "https://www.tiktok.com/@hotspotfinders/video/7509923486517824790"
        )

    def test_tiktok_video_path_unchanged_except_for_query(self) -> None:
        url = "https://www.tiktok.com/@user/video/123"
        assert canonicalize_url(url) == url

    def test_photo_path_rewrite_is_case_insensitive(self) -> None:
        url = "https://www.tiktok.com/@user/Photo/123"
        assert canonicalize_url(url) == "https://www.tiktok.com/@user/video/123"

    def test_non_tiktok_url_unchanged_by_photo_rewrite(self) -> None:
        # Instagram `/p/abc/` is its own thing; the photo→video rewrite
        # is TikTok-only.
        url = "https://www.instagram.com/p/abc/"
        assert canonicalize_url(url) == "https://www.instagram.com/p/abc"


class TestUnknownHostPassthrough:
    def test_unknown_host_returns_input_unchanged(self) -> None:
        url = "https://example.com/page?utm_source=x&utm_medium=y#section"
        assert canonicalize_url(url) == url

    def test_youtu_be_shortlink_passes_through(self) -> None:
        # `youtu.be` is intentionally NOT in the canonical-host set — it's
        # a shortlink whose `?t=42` (seek offset) is load-bearing for
        # the user-shared timestamp. Resolution + canonicalization is a
        # follow-up.
        url = "https://youtu.be/abc?t=42"
        assert canonicalize_url(url) == url

    def test_maps_shortlink_passes_through(self) -> None:
        # Same reasoning — shortlink resolution is a follow-up.
        url = "https://maps.app.goo.gl/abc?si=xyz"
        assert canonicalize_url(url) == url


class TestNullHandling:
    def test_none_returns_none(self) -> None:
        assert canonicalize_url(None) is None


@pytest.mark.parametrize(
    "raw,expected_host,expected_path",
    [
        ("https://tiktok.com/@u/video/1", "tiktok.com", "/@u/video/1"),
        ("HTTPS://tiktok.com/@u/video/1", "tiktok.com", "/@u/video/1"),
        ("https://Www.TIKTOK.com/@u/video/1/", "www.tiktok.com", "/@u/video/1"),
    ],
)
def test_scheme_and_host_case_insensitivity(
    raw: str, expected_host: str, expected_path: str
) -> None:
    from urllib.parse import urlparse

    out = canonicalize_url(raw)
    assert out is not None
    parsed = urlparse(out)
    assert parsed.scheme == "https"
    assert parsed.hostname == expected_host
    assert parsed.path == expected_path
    assert parsed.query == ""
    assert parsed.fragment == ""
