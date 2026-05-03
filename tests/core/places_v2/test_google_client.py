"""Tests for GooglePlacesClient — search routing and query-to-text translation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from totoro_ai.core.places_v2._google_query_builder import build_text_search_params
from totoro_ai.core.places_v2.google_client import (
    _DETAILS_CONCURRENCY,
    GooglePlacesClient,
)
from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCategory,
    PlaceObject,
    PlaceQuery,
)
from totoro_ai.core.places_v2.tags import (
    AccessibilityTag,
    AtmosphereTag,
    CuisineTag,
    DietaryTag,
    FeatureTag,
    SeasonTag,
    ServiceTag,
    TimeTag,
)


def _make_client() -> GooglePlacesClient:
    client = GooglePlacesClient(
        api_key="test", http=MagicMock(spec=httpx.AsyncClient)
    )
    client._text_search = AsyncMock(return_value=[])
    client._nearby_search = AsyncMock(return_value=[])
    return client


# ---------------------------------------------------------------------------
# search() routing
# ---------------------------------------------------------------------------

class TestSearchRouting:
    async def test_category_routes_to_text_search(self) -> None:
        c = _make_client()
        await c.search(PlaceQuery(category=PlaceCategory.restaurant), limit=5)
        c._text_search.assert_awaited_once()
        c._nearby_search.assert_not_awaited()

    async def test_cuisine_tag_routes_to_text_search(self) -> None:
        c = _make_client()
        await c.search(PlaceQuery(place_name="Thai food"), limit=5)
        c._text_search.assert_awaited_once()
        c._nearby_search.assert_not_awaited()

    async def test_geo_only_routes_to_nearby_search(self) -> None:
        c = _make_client()
        await c.search(
            PlaceQuery(location=LocationContext(lat=13.7, lng=100.5, radius_m=500)),
            limit=5,
        )
        c._nearby_search.assert_awaited_once()
        c._text_search.assert_not_awaited()

    async def test_skip_tags_only_with_geo_falls_back_to_nearby(self) -> None:
        """Skip-only tags produce no text → falls back to nearby when geo present."""
        c = _make_client()
        await c.search(
            PlaceQuery(
                tags=[TimeTag.evening, AccessibilityTag.wheelchair_entrance],
                location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
            ),
            limit=5,
        )
        c._nearby_search.assert_awaited_once()
        c._text_search.assert_not_awaited()

    async def test_skip_tags_no_geo_returns_empty(self) -> None:
        c = _make_client()
        result = await c.search(
            PlaceQuery(tags=[SeasonTag.summer, TimeTag.evening]), limit=5
        )
        assert result == []
        c._text_search.assert_not_awaited()
        c._nearby_search.assert_not_awaited()

    async def test_empty_query_returns_empty(self) -> None:
        c = _make_client()
        result = await c.search(PlaceQuery(), limit=5)
        assert result == []
        c._text_search.assert_not_awaited()
        c._nearby_search.assert_not_awaited()

    async def test_tags_with_geo_uses_text_search(self) -> None:
        """Searchable tags + geo → text search (not nearby) so tags are applied."""
        c = _make_client()
        await c.search(
            PlaceQuery(
                tags=[CuisineTag.thai],
                location=LocationContext(lat=13.7, lng=100.5, radius_m=500),
            ),
            limit=5,
        )
        c._text_search.assert_awaited_once()
        passed_query: PlaceQuery = c._text_search.call_args.args[0]
        assert passed_query.location is not None
        c._nearby_search.assert_not_awaited()


# ---------------------------------------------------------------------------
# build_text_search_params — text/type dedup
# ---------------------------------------------------------------------------

class TestBuildTextSearchParams:
    def test_strips_type_mapped_tag_from_text_when_other_text_remains(self) -> None:
        q = PlaceQuery(place_name="ramen", tags=[CuisineTag.thai])
        text, included_type = build_text_search_params(q)
        assert text == "ramen"
        assert included_type == "thai_restaurant"

    def test_strips_type_mapped_category_from_text_when_other_text_remains(
        self,
    ) -> None:
        q = PlaceQuery(category=PlaceCategory.restaurant, tags=[DietaryTag.vegan])
        text, included_type = build_text_search_params(q)
        assert text == "vegan"
        assert included_type == "restaurant"

    def test_keeps_type_mapped_term_in_text_when_it_would_be_only_text(self) -> None:
        # Stripping would leave textQuery empty — Google rejects that.
        q = PlaceQuery(category=PlaceCategory.cafe)
        text, included_type = build_text_search_params(q)
        assert text == "cafe"
        assert included_type == "cafe"

    def test_keeps_type_mapped_tag_when_it_would_be_only_text(self) -> None:
        q = PlaceQuery(tags=[CuisineTag.thai])
        text, included_type = build_text_search_params(q)
        assert text == "Thai"
        assert included_type == "thai_restaurant"

    def test_category_takes_precedence_as_includedType(self) -> None:
        # Category checked before tags — its type wins the includedType slot.
        q = PlaceQuery(
            place_name="ramen",
            category=PlaceCategory.restaurant,
            tags=[CuisineTag.thai],
        )
        text, included_type = build_text_search_params(q)
        assert included_type == "restaurant"
        # Category is stripped; thai stays in text.
        assert "restaurant" not in text
        assert "Thai" in text
        assert "ramen" in text

    def test_unmapped_tag_falls_through_to_text(self) -> None:
        # FeatureTag has no entry in _TAG_TO_GOOGLE_TYPE — goes to text only.
        q = PlaceQuery(place_name="cafe", tags=[FeatureTag.outdoor_seating])
        text, included_type = build_text_search_params(q)
        assert included_type is None
        assert "cafe" in text
        assert "outdoor seating" in text

    def test_skip_tags_excluded_entirely(self) -> None:
        q = PlaceQuery(place_name="park", tags=[TimeTag.late_night])
        text, included_type = build_text_search_params(q)
        assert text == "park"
        assert included_type is None

    def test_empty_query_returns_empty_text_and_no_type(self) -> None:
        text, included_type = build_text_search_params(PlaceQuery())
        assert text == ""
        assert included_type is None

    def test_place_name_only(self) -> None:
        q = PlaceQuery(place_name="ramen near Shibuya")
        text, included_type = build_text_search_params(q)
        assert text == "ramen near Shibuya"
        assert included_type is None

    def test_underscores_in_unmapped_tags_converted_to_spaces(self) -> None:
        # ServiceTag and AtmosphereTag have no Google type mapping → fall to text.
        q = PlaceQuery(
            place_name="bar",
            tags=[ServiceTag.serves_cocktails, AtmosphereTag.laid_back],
        )
        text, _ = build_text_search_params(q)
        assert "serves cocktails" in text
        assert "laid back" in text

    def test_deduplicates_repeated_parts(self) -> None:
        # place_name and a tag with the same value collapse to a single token.
        q = PlaceQuery(place_name="Ramen", tags=["Ramen"])
        text, _ = build_text_search_params(q)
        assert text.count("Ramen") == 1


# ---------------------------------------------------------------------------
# get_by_ids() — Place Details fan-out
# ---------------------------------------------------------------------------

def _stub_place(provider_id: str) -> PlaceObject:
    return PlaceObject(
        provider_id=provider_id,
        place_name=provider_id.split(":", 1)[1],
        location=LocationContext(lat=1.0, address="Test St"),
    )


class TestGetByIds:
    async def test_empty_returns_empty(self) -> None:
        c = _make_client()
        c._get_details = AsyncMock()
        assert await c.get_by_ids([]) == []
        c._get_details.assert_not_awaited()

    async def test_drops_failed_lookups(self) -> None:
        c = _make_client()
        c._get_details = AsyncMock(
            side_effect=[_stub_place("google:a"), None, _stub_place("google:c")]
        )
        result = await c.get_by_ids(["google:a", "google:b", "google:c"])
        assert [r.provider_id for r in result] == ["google:a", "google:c"]

    async def test_concurrency_capped(self) -> None:
        """get_by_ids fan-out is bounded by _DETAILS_CONCURRENCY."""
        n = _DETAILS_CONCURRENCY * 3
        in_flight = 0
        max_in_flight = 0

        async def slow_details(provider_id: str) -> PlaceObject | None:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                # Yield twice so concurrent callers can pile up to the cap.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return _stub_place(provider_id)
            finally:
                in_flight -= 1

        c = _make_client()
        c._get_details = AsyncMock(side_effect=slow_details)

        await c.get_by_ids([f"google:p{i}" for i in range(n)])

        assert c._get_details.await_count == n
        assert max_in_flight <= _DETAILS_CONCURRENCY

    async def test_unsupported_provider_returns_none(self) -> None:
        """_get_details rejects non-google provider_ids without a network call."""
        c = _make_client()
        c._request = AsyncMock()  # would explode if called
        result = await c._get_details("foursquare:abc")
        assert result is None
        c._request.assert_not_awaited()
