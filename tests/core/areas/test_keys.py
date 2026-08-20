"""Tests for the area id codec (ADR-153, id-grammar since the geo registry).

The codec's contract: every canonical geo key round-trips through one
URL-safe segment; anything that is not a token this codec minted — or that
decodes to a non-key — raises rather than leaking garbage into a query.
Tokens minted before the id migration decode to the old slug grammar and
are flagged legacy so the route can translate them through the registry.
"""

from __future__ import annotations

import pytest

from kebi.core.areas.keys import (
    decode_area_id,
    encode_area_id,
    is_geo_key,
    is_legacy_geo_key,
    parent_keys,
)

_CANGGU_KEY = "id/CityBali01/AreaCanggu01"


class TestRoundTrip:
    @pytest.mark.parametrize(
        "key",
        [
            "id",
            "id/CityBali01",
            _CANGGU_KEY,
            "th/ChIJ82ENKDJgHTERIEjiXbIAAQE",
            # Legacy slug keys still encode and round-trip — old data and
            # old tokens share one codec.
            "id/bali/canggu",
            "vn/ho-chi-minh-city",
        ],
    )
    def test_every_key_level_round_trips(self, key: str) -> None:
        assert decode_area_id(encode_area_id(key)) == key

    def test_token_is_one_url_safe_segment(self) -> None:
        token = encode_area_id(_CANGGU_KEY)
        assert "/" not in token
        assert "=" not in token

    def test_stray_slashes_are_tolerated_on_encode(self) -> None:
        assert decode_area_id(encode_area_id("/id/CityBali01/")) == "id/CityBali01"


class TestRejection:
    @pytest.mark.parametrize(
        "bad_key",
        [
            "",
            "indonesia/CityBali01",  # not an alpha-2 country
            "ID/CityBali01",  # country code is lowercase
            "id/CityBali01/AreaCanggu01/deeper",  # deeper than the grammar
            "id//AreaCanggu01",  # empty segment
        ],
    )
    def test_non_canonical_keys_are_never_encoded(self, bad_key: str) -> None:
        with pytest.raises(ValueError):
            encode_area_id(bad_key)

    @pytest.mark.parametrize(
        "bad_token",
        [
            "not base64!!",
            "id/CityBali01",  # a raw key is not a token
            encode_area_id("id/CityBali01") + "-",  # corrupted tail
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


class TestLegacyDetection:
    """A pre-migration token decodes to an all-lowercase slug path; the
    route translates those through the registry's recorded legacy keys."""

    @pytest.mark.parametrize("key", ["id/bali", "id/bali/canggu", "vn/da-nang"])
    def test_slug_keys_are_legacy(self, key: str) -> None:
        assert is_legacy_geo_key(key)

    @pytest.mark.parametrize(
        "key",
        [
            _CANGGU_KEY,
            "id/CityBali01",
            # A bare country code means the same thing in both eras — no
            # translation needed, so it is not legacy.
            "id",
        ],
    )
    def test_id_keys_and_country_keys_are_not_legacy(self, key: str) -> None:
        assert not is_legacy_geo_key(key)

    def test_both_grammars_are_valid_geo_keys(self) -> None:
        assert is_geo_key("id/bali/canggu")
        assert is_geo_key(_CANGGU_KEY)
        assert not is_geo_key("ID/Bali")


class TestHelpers:
    def test_parent_keys_outermost_first(self) -> None:
        assert parent_keys(_CANGGU_KEY) == ["id", "id/CityBali01"]
        assert parent_keys("id/CityBali01") == ["id"]
        assert parent_keys("id") == []
