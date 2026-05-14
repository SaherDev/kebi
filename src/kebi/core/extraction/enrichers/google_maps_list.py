"""Google Maps shared-list enricher (Apify-backed) — pure text producer.

Google Maps shared lists (`maps.app.goo.gl/<short>` that resolves to a
URL with `!3e3` in its data parameter) cannot be scraped from plain
HTML — the page is fully client-side rendered. We delegate to the
Apify `parseforge/google-maps-shared-list-scraper` actor, which runs
the scrape and returns each list entry with its name, lat/lng,
rating, and category.

This enricher is a **pure text producer** — it appends each Apify
item's name to `context.known_places` and stops. The pipeline's NER
finalizer (`LLMNEREnricher`) reads that list as another text source
and emits structured `CandidatePlace`s with inferred `place_type`,
`subcategory`, `cuisine`, and other attributes. That's the same
path subtitle/whisper text takes — one consolidator owns the "name
→ structured PlaceCreate" step.

Notes:
- Apify returns a Google Maps internal FID (`0x...:0x...`), not a
  Places API ChIJ Place ID. We drop it; the downstream validator
  resolves the canonical Place ID via name + city.
- The actor's default proxy (`useApifyProxy: true` → residential) is
  a paid feature; we pass `useApifyProxy: false` to use the actor's
  built-in fallback path that works on free-tier accounts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from kebi.core.config import get_env
from kebi.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from kebi.core.extraction.types import (
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
)
from kebi.core.places_v2 import PlaceSource

logger = logging.getLogger(__name__)

_APIFY_ENDPOINT = (
    "https://api.apify.com/v2/acts/"
    "parseforge~google-maps-shared-list-scraper/run-sync-get-dataset-items"
)
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_PLACES = 100


class GoogleMapsListEnricher(SourceFilteredEnricher):
    """Pulls a Google Maps shared list via the Apify scraper actor.

    Gated to `PlaceSource.google_maps_list`. Skips silently when the Apify
    token isn't configured (no candidates appended; the rest of the
    cascade keeps running). Exceptions propagate to the surrounding
    `CircuitBreakerEnricher` so a degraded Apify doesn't keep retrying
    on every request.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        token: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(allowed_sources={PlaceSource.google_maps_list})
        # Lazy-resolve the token so tests can construct the enricher
        # without touching the env, but production callers can pass it
        # explicitly if they want.
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._http = http

    def _resolve_token(self) -> str | None:
        return self._token or get_env().APIFY_TOKEN

    async def _run(self, context: ExtractionContext) -> None:
        token = self._resolve_token()
        if not token:
            logger.info(
                "GoogleMapsListEnricher skipped — APIFY_TOKEN not configured (url=%s)",
                context.url,
            )
            return

        items = await self._fetch_list(context.url, token)  # type: ignore[arg-type]
        for item in items:
            name = item.get("name") or item.get("title")
            if name:
                name_str = str(name)
                context.known_places.append(
                    KnownPlace(
                        name=name_str,
                        producer=Producer.GOOGLE_MAPS_LIST,
                        medium=Medium.LIST,
                        snippet=name_str,
                    )
                )

    async def _fetch_list(self, url: str, token: str) -> list[dict[str, Any]]:
        body = {
            "listUrls": [url],
            "outputFormat": "json",
            "maxPlacesPerList": _DEFAULT_MAX_PLACES,
            # Skip Apify's residential proxy — it's a paid-tier feature
            # and the actor falls back to a working scraping path
            # without it.
            "proxyConfiguration": {"useApifyProxy": False},
        }
        response = await self._http.post(
            _APIFY_ENDPOINT,
            params={"token": token},
            json=body,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return []
        return data
