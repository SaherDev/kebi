"""Tests for GooglePlacesClient match-quality classification logic."""

from unittest.mock import AsyncMock, MagicMock, patch

from kebi.core.places.places_client import (
    GooglePlacesClient,
    PlacesMatchQuality,
    _map_opening_hours,
)


def _fake_response(name: str, place_id: str = "place_123") -> MagicMock:
    """Build a fake httpx.Response wrapping a minimal Places API payload."""
    payload = {
        "candidates": [
            {
                "name": name,
                "place_id": place_id,
                "geometry": {"location": {"lat": 13.7, "lng": 100.5}},
            }
        ]
    }
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _patched_client() -> GooglePlacesClient:
    """Return a GooglePlacesClient with secrets + config mocked out."""
    with (
        patch("kebi.core.places.places_client.get_env") as mock_env,
        patch("kebi.core.places.places_client.get_config") as mock_config,
    ):
        mock_env.return_value.GOOGLE_API_KEY = "fake-key"
        cfg = MagicMock()
        cfg.external_services.google_places.base_url = (
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
        )
        cfg.external_services.google_places.timeout_seconds = 5.0
        cfg.external_services.google_places.request_fields = [
            "name",
            "place_id",
            "geometry",
        ]
        mock_config.return_value = cfg
        return GooglePlacesClient()


# ---------------------------------------------------------------------------
# Classification: EXACT
# ---------------------------------------------------------------------------


async def test_identical_name_is_exact() -> None:
    """Candidate name matching Google name exactly → EXACT."""
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place("Ramen Kaisugi", location=None)

    assert result.match_quality == PlacesMatchQuality.EXACT


async def test_case_insensitive_name_is_exact() -> None:
    """Candidate name in ALL CAPS matching Google name → EXACT."""
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place("RAMEN KAISUGI", location=None)

    assert result.match_quality == PlacesMatchQuality.EXACT


async def test_name_with_city_suffix_is_fuzzy() -> None:
    """City suffix in candidate name lowers ratio from 1.0 → ~0.77 (FUZZY).

    Current `_normalize` trusts the LLM's structured output and does not
    noise-strip. "ramen kaisugi bangkok" vs "ramen kaisugi" → ratio 0.77,
    which falls in the FUZZY band (0.70–0.85).
    """
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place(
            "RAMEN KAISUGI Bangkok", location="Bangkok"
        )

    assert result.match_quality == PlacesMatchQuality.FUZZY


async def test_name_with_wrong_city_field_still_fuzzy() -> None:
    """Wrong-city field does not affect the normalize step — still FUZZY.

    The `location` param is not used in the comparison path at all; only
    `name` feeds `_normalize`. So this test has the same outcome as above.
    """
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place(
            "RAMEN KAISUGI Bangkok", location="Sukhumvit 33"
        )

    assert result.match_quality == PlacesMatchQuality.FUZZY


async def test_name_with_street_in_candidate_name_is_category_only() -> None:
    """Street tokens inflate the candidate length enough to drop into CATEGORY_ONLY.

    "ramen kaisugi sukhumvit 33" vs "ramen kaisugi" → ratio 0.67, which
    falls in the CATEGORY_ONLY band (0.35–0.70). This is the expected
    penalty for NER leaking location noise into the place_name field —
    downstream callers treat CATEGORY_ONLY as "accept category, not name".
    """
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place(
            "RAMEN KAISUGI Sukhumvit 33", location=None
        )

    assert result.match_quality == PlacesMatchQuality.CATEGORY_ONLY


async def test_emoji_prefix_candidate_name_is_fuzzy() -> None:
    """Same candidate shape as the city-suffix test → FUZZY."""
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place(
            "RAMEN KAISUGI Bangkok", location="Bangkok"
        )

    assert result.match_quality == PlacesMatchQuality.FUZZY


# ---------------------------------------------------------------------------
# Classification: EXACT (high-similarity near-match)
# ---------------------------------------------------------------------------


async def test_close_but_not_exact_is_exact() -> None:
    """SequenceMatcher over full lowercased strings.

    "fuji ramen" vs "fujiya ramen" → ratio 0.909, which is ≥ 0.85 → EXACT.
    The old test asserted FUZZY under a core-token scheme that stripped
    shared tokens; the current simpler scheme gives this pair an EXACT
    classification.
    """
    client = _patched_client()
    response = _fake_response("Fujiya Ramen")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place("Fuji Ramen", location=None)

    assert result.match_quality == PlacesMatchQuality.EXACT


# ---------------------------------------------------------------------------
# Classification: CATEGORY_ONLY
# ---------------------------------------------------------------------------


async def test_unrelated_name_is_category_only() -> None:
    """Low core-token similarity → CATEGORY_ONLY."""
    client = _patched_client()
    response = _fake_response("Nakatani Sushi")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place("Ramen Kaisugi", location=None)

    assert result.match_quality == PlacesMatchQuality.CATEGORY_ONLY


# ---------------------------------------------------------------------------
# No candidates → NONE
# ---------------------------------------------------------------------------


async def test_no_candidates_returns_none_quality() -> None:
    """Empty candidates list → NONE match quality."""
    client = _patched_client()
    resp = MagicMock()
    resp.json.return_value = {"candidates": []}
    resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await client.validate_place("Unknown Place", location=None)

    assert result.match_quality == PlacesMatchQuality.NONE
    assert result.external_id is None


async def test_formatted_address_read_from_response() -> None:
    """Google's `formatted_address` field is surfaced on PlacesMatchResult
    so the persistence layer can write it to the Tier 2 geo cache."""
    client = _patched_client()
    payload = {
        "candidates": [
            {
                "name": "Ramen Kaisugi",
                "place_id": "place_123",
                "geometry": {"location": {"lat": 13.7, "lng": 100.5}},
                "formatted_address": "1 Sukhumvit Rd, Bangkok 10110, Thailand",
            }
        ]
    }
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=resp):
        result = await client.validate_place("Ramen Kaisugi", location=None)

    assert result.address == "1 Sukhumvit Rd, Bangkok 10110, Thailand"
    assert result.lat == 13.7
    assert result.lng == 100.5


async def test_missing_formatted_address_leaves_address_none() -> None:
    """When Google omits formatted_address, PlacesMatchResult.address is None."""
    client = _patched_client()
    response = _fake_response("Ramen Kaisugi")  # no formatted_address
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=response):
        result = await client.validate_place("Ramen Kaisugi", location=None)

    assert result.address is None


async def test_get_place_details_passes_language_code_en() -> None:
    """v1 `get_place_details` must set `languageCode=en` on the request
    so `formattedAddress` comes back in English regardless of server IP
    locale inference. Without this, addresses drift into the server's
    default locale (e.g. "Tayland" / "Japonya" — Turkish for Thailand
    / Japan)."""
    client = _patched_client()
    resp = MagicMock()
    resp.json.return_value = {
        "location": {"latitude": 13.75, "longitude": 100.5},
        "formattedAddress": "1 Sukhumvit Rd, Bangkok 10110, Thailand",
    }
    resp.raise_for_status = MagicMock()

    mock_get = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient.get", new=mock_get):
        result = await client.get_place_details("ChIJxxx")

    assert result is not None
    assert result["address"] == "1 Sukhumvit Rd, Bangkok 10110, Thailand"
    # The request must have passed languageCode=en in the params.
    call_kwargs = mock_get.await_args.kwargs
    assert call_kwargs["params"] == {"languageCode": "en"}


# ---------------------------------------------------------------------------
# _map_opening_hours — v1 Places API shape
# ---------------------------------------------------------------------------
# The v1 Places API carries open/close in each period as separate integer
# `hour` and `minute` fields (NOT a classic-style "HHMM" string). Regression
# test for the schema mismatch that produced literal ":-:" strings on every
# day before the fix.


def test_map_opening_hours_formats_v1_periods_as_time_ranges() -> None:
    response = {
        "regularOpeningHours": {
            "periods": [
                # Monday 09:00 - 22:30
                {
                    "open": {"day": 1, "hour": 9, "minute": 0},
                    "close": {"day": 1, "hour": 22, "minute": 30},
                },
                # Wednesday 12:05 - 23:00
                {
                    "open": {"day": 3, "hour": 12, "minute": 5},
                    "close": {"day": 3, "hour": 23, "minute": 0},
                },
            ]
        },
        "timeZone": {"id": "Asia/Bangkok"},
    }

    hours = _map_opening_hours(response)

    assert hours is not None
    assert hours["monday"] == "09:00-22:30"
    assert hours["wednesday"] == "12:05-23:00"
    # Days without a period are None (closed).
    assert hours["tuesday"] is None
    assert hours["thursday"] is None
    assert hours["timezone"] == "Asia/Bangkok"


def test_map_opening_hours_handles_midnight_close() -> None:
    """A place closing at midnight is encoded as close.hour=0 in v1.

    Regression fixture for Thipsamai ("09:00-00:00" for every day).
    """
    response = {
        "regularOpeningHours": {
            "periods": [
                {
                    "open": {"day": 0, "hour": 9, "minute": 0},
                    "close": {"day": 1, "hour": 0, "minute": 0},
                },
            ]
        },
        "timeZone": {"id": "Asia/Bangkok"},
    }

    hours = _map_opening_hours(response)

    assert hours is not None
    assert hours["sunday"] == "09:00-00:00"


def test_map_opening_hours_open_without_close_is_24h() -> None:
    """A period with `open` and no `close` means the place is open 24h."""
    response = {
        "regularOpeningHours": {
            "periods": [
                {"open": {"day": 5, "hour": 0, "minute": 0}},  # no close
            ]
        },
        "timeZone": {"id": "UTC"},
    }

    hours = _map_opening_hours(response)

    assert hours is not None
    assert hours["friday"] == "00:00-00:00"


def test_map_opening_hours_requires_timezone() -> None:
    """A response without a timezone cannot build a valid HoursDict."""
    response = {
        "regularOpeningHours": {
            "periods": [
                {
                    "open": {"day": 1, "hour": 9, "minute": 0},
                    "close": {"day": 1, "hour": 17, "minute": 0},
                },
            ]
        },
        # No timeZone field
    }

    assert _map_opening_hours(response) is None


def test_map_opening_hours_missing_hour_minute_defaults_to_zero() -> None:
    """Degraded responses where hour/minute are absent default to 0,
    producing `"00:00"` rather than malformed strings."""
    response = {
        "regularOpeningHours": {
            "periods": [
                {
                    "open": {"day": 2},  # no hour/minute
                    "close": {"day": 2, "hour": 17, "minute": 30},
                },
            ]
        },
        "timeZone": {"id": "UTC"},
    }

    hours = _map_opening_hours(response)

    assert hours is not None
    assert hours["tuesday"] == "00:00-17:30"
