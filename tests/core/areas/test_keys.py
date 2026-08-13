"""Tests for the area id codec (ADR-153).

The codec's contract: every canonical geo key round-trips through one
URL-safe segment; anything that is not a token this codec minted — or that
decodes to a non-key — raises rather than leaking garbage into a query.
"""

from __future__ import annotations

import pytest

from kebi.core.areas.keys import (
    decode_area_id,
    display_from_slug,
    encode_area_id,
    parent_keys,
)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "key", ["id", "id/bali", "id/bali/canggu", "vn/ho-chi-minh-city", "jp/tokyo"]
    )
    def test_every_key_level_round_trips(self, key: str) -> None:
        assert decode_area_id(encode_area_id(key)) == key

    def test_token_is_one_url_safe_segment(self) -> None:
        token = encode_area_id("id/bali/canggu")
        assert "/" not in token
        assert "=" not in token

    def test_stray_slashes_are_tolerated_on_encode(self) -> None:
        assert decode_area_id(encode_area_id("/id/bali/")) == "id/bali"


class TestRejection:
    @pytest.mark.parametrize(
        "bad_key",
        [
            "",
            "indonesia/bali",  # not an alpha-2 country
            "ID/Bali",  # uppercase never leaves build_geo_key
            "id/bali/canggu/deeper",  # deeper than the grammar
            "id//canggu",  # empty segment
        ],
    )
    def test_non_canonical_keys_are_never_encoded(self, bad_key: str) -> None:
        with pytest.raises(ValueError):
            encode_area_id(bad_key)

    @pytest.mark.parametrize(
        "bad_token",
        [
            "not base64!!",
            "id/bali",  # a raw key is not a token
            encode_area_id("id/bali") + "x",  # corrupted tail
            "",
        ],
    )
    def test_garbage_tokens_are_rejected(self, bad_token: str) -> None:
        with pytest.raises(ValueError):
            decode_area_id(bad_token)

    def test_valid_base64_of_a_non_key_is_rejected(self) -> None:
        import base64

        token = base64.urlsafe_b64encode(b"DROP TABLE areas").decode().rstrip("=")
        with pytest.raises(ValueError):
            decode_area_id(token)


class TestHelpers:
    def test_parent_keys_outermost_first(self) -> None:
        assert parent_keys("id/bali/canggu") == ["id", "id/bali"]
        assert parent_keys("id/bali") == ["id"]
        assert parent_keys("id") == []

    def test_display_from_slug(self) -> None:
        assert display_from_slug("canggu") == "Canggu"
        assert display_from_slug("ho-chi-minh-city") == "Ho Chi Minh City"
        # A bare country code has no derivable name — the profiler's dressing
        # replaces this; until then it shows as the code.
        assert display_from_slug("id") == "ID"
