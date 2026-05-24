"""Tests for GoogleMapsListEnricher (Apify-backed)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kebi.core.extraction.enrichers.google_maps_list import (
    GoogleMapsListEnricher,
)
from kebi.core.extraction.types import ExtractionContext
from kebi.core.places import PlaceSource


def _ctx(url: str = "https://maps.app.goo.gl/9KPNCHsoi5s69xE59") -> ExtractionContext:
    return ExtractionContext(url=url, user_id="u1")


def _mock_response(payload: object, status: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    # Apify sync endpoint returns no run-id; pagination header is the
    # only available item-count signal — wrap reads it to compute cost.
    count = len(payload) if isinstance(payload, list) else 0
    response.headers = {"x-apify-pagination-total": str(count)}
    return response


def _mock_http(post: Any) -> AsyncMock:
    """Build an injectable AsyncClient mock whose `.post` runs `post`."""
    http = AsyncMock(spec=httpx.AsyncClient)
    http.post = post
    return http


class TestSourceGate:
    async def test_skips_when_source_is_not_google_maps(self) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = GoogleMapsListEnricher(token="t", http=http)
        ctx = ExtractionContext(url="https://tiktok.com/v/123", user_id="u1")
        await enricher.enrich(ctx)
        http.post.assert_not_awaited()
        assert ctx.known_places == []

    async def test_skips_when_no_url(self) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = GoogleMapsListEnricher(token="t", http=http)
        ctx = ExtractionContext(url=None, user_id="u1")
        await enricher.enrich(ctx)
        http.post.assert_not_awaited()


class TestTokenResolution:
    async def test_skips_silently_when_token_missing(self) -> None:
        http = AsyncMock(spec=httpx.AsyncClient)
        enricher = GoogleMapsListEnricher(token=None, http=http)
        with patch.object(enricher, "_resolve_token", return_value=None):
            await enricher.enrich(_ctx())
        http.post.assert_not_awaited()


class TestApifyResponse:
    async def test_appends_each_name_to_known_places(self) -> None:
        """Name producer — names land in known_places. The pipeline's
        searcher consumes them as queries, the picker chooses + classifies."""
        items = [
            {"name": "Joe's Pizza", "placeId": "0xabc:0x123"},
            {"name": "Eleven Madison Park", "placeId": "0xdef:0x456"},
        ]
        ctx = _ctx()

        async def _post(*_a, **_kw):  # type: ignore[no-untyped-def]
            return _mock_response(items)

        enricher = GoogleMapsListEnricher(token="apify-token", http=_mock_http(_post))
        await enricher.enrich(ctx)

        assert [k.name for k in ctx.known_places] == [
            "Joe's Pizza",
            "Eleven Madison Park",
        ]
        assert all(k.producer.value == "google_maps_list" for k in ctx.known_places)
        assert all(k.medium.value == "list" for k in ctx.known_places)

    async def test_skips_items_without_a_name(self) -> None:
        items = [
            {"placeId": "0xnoname:0x"},  # no name
            {"name": "Joe's Pizza", "placeId": "0xabc:0x123"},
        ]
        ctx = _ctx()

        async def _post(*_a, **_kw):  # type: ignore[no-untyped-def]
            return _mock_response(items)

        enricher = GoogleMapsListEnricher(token="apify-token", http=_mock_http(_post))
        await enricher.enrich(ctx)

        assert [k.name for k in ctx.known_places] == ["Joe's Pizza"]

    async def test_request_body_disables_apify_residential_proxy(self) -> None:
        """Apify residential proxy is a paid-tier feature; the enricher
        must always send useApifyProxy=False so free-tier accounts work."""
        captured: dict[str, Any] = {}

        async def _post(*_a, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return _mock_response([])

        enricher = GoogleMapsListEnricher(token="apify-token", http=_mock_http(_post))
        await enricher.enrich(_ctx())

        body = captured["json"]
        assert body["listUrls"] == ["https://maps.app.goo.gl/9KPNCHsoi5s69xE59"]
        assert body["outputFormat"] == "json"
        assert body["proxyConfiguration"] == {"useApifyProxy": False}

    async def test_apify_http_error_propagates_to_circuit_breaker(self) -> None:
        """Errors must NOT be caught here — the surrounding CircuitBreakerEnricher
        owns the retry/back-off bookkeeping."""

        async def _post(*_a, **_kw):  # type: ignore[no-untyped-def]
            raise httpx.HTTPError("apify down")

        enricher = GoogleMapsListEnricher(token="apify-token", http=_mock_http(_post))
        with pytest.raises(httpx.HTTPError):
            await enricher.enrich(_ctx())


class TestSourceGateMembership:
    def test_allowed_sources_is_google_maps_only(self) -> None:
        enricher = GoogleMapsListEnricher(
            token="t", http=AsyncMock(spec=httpx.AsyncClient)
        )
        assert enricher.allowed_sources == frozenset({PlaceSource.google_maps_list})
