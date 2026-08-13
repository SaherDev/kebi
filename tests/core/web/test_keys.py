"""Tests for the web URL codec — `kebi://web/{token}` payloads.

Same discipline as the area codec (ADR-153): reversible, URL-safe, and
refuses to mint or decode anything that is not an http(s) page URL.
"""

from __future__ import annotations

import pytest

from kebi.core.web.keys import decode_web_url, encode_web_url


class TestRoundTrip:
    def test_round_trips_a_page_url(self) -> None:
        url = "https://fifa.com/en/tournaments/mens/worldcup/schedule?stage=group"
        assert decode_web_url(encode_web_url(url)) == url

    def test_http_is_accepted(self) -> None:
        url = "http://example.com/page"
        assert decode_web_url(encode_web_url(url)) == url

    def test_token_is_one_url_safe_segment(self) -> None:
        token = encode_web_url("https://fifa.com/schedule?a=1&b=2")
        assert "/" not in token
        assert "=" not in token
        assert "+" not in token


class TestRefusals:
    def test_encode_rejects_non_http_schemes(self) -> None:
        for bad in ("mailto:x@example.com", "javascript:alert(1)", "fifa.com"):
            with pytest.raises(ValueError):
                encode_web_url(bad)

    def test_decode_rejects_foreign_tokens(self) -> None:
        with pytest.raises(ValueError):
            decode_web_url("not-base64!!")

    def test_decode_rejects_a_token_for_a_non_url(self) -> None:
        # Well-formed base64 whose payload is not an http(s) URL — e.g. an
        # area token pasted into a web link.
        import base64

        token = base64.urlsafe_b64encode(b"id/bali/canggu").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_web_url(token)
