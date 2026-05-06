"""Places validator — parallel multi-candidate validation with confidence scoring.

Takes extraction-only `CandidatePlace`s and returns `ValidatedCandidate`s
with `provider` + `external_id` resolved from Google Places, plus a
confidence score and the Tier 2 geo data the persistence layer needs.
The persistence layer is the only place that builds a `PlaceCreate` —
this module never touches the write-side shape.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from totoro_ai.core.config import ConfidenceConfig
from totoro_ai.core.extraction.confidence import calculate_confidence
from totoro_ai.core.extraction.types import (
    CandidatePlace,
    ValidatedCandidate,
)
from totoro_ai.core.places import (
    PlaceProvider,
    PlacesClient,
    PlacesMatchQuality,
    PlacesMatchResult,
)

# Google Places types that indicate a geographic feature, not a venue.
# Candidates that resolve to any of these types are rejected post-validation.
_GEOGRAPHIC_PLACE_TYPES: frozenset[str] = frozenset(
    {
        "route",
        "street_address",
        "political",
        "locality",
        "sublocality",
        "sublocality_level_1",
        "sublocality_level_2",
        "sublocality_level_3",
        "sublocality_level_4",
        "sublocality_level_5",
        "country",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "administrative_area_level_4",
        "administrative_area_level_5",
        "neighborhood",
        "postal_code",
        "intersection",
        "premise",
        "natural_feature",
    }
)

# Match-quality → modifier mapping (ADR-029, plan.md Phase 5)
_QUALITY_MODIFIERS: dict[PlacesMatchQuality, float] = {
    PlacesMatchQuality.EXACT: 1.0,
    PlacesMatchQuality.FUZZY: 0.9,
    PlacesMatchQuality.CATEGORY_ONLY: 0.8,
    PlacesMatchQuality.NONE: 0.3,
}

# Google's external_provider strings we know how to namespace.
_PROVIDER_MAP: dict[str, PlaceProvider] = {
    "google": PlaceProvider.google,
    "foursquare": PlaceProvider.foursquare,
    "manual": PlaceProvider.manual,
}


class PlacesValidatorProtocol(Protocol):
    """Protocol for swappable place-registry validators (ADR-038)."""

    async def validate(
        self, candidates: list[CandidatePlace]
    ) -> list[ValidatedCandidate] | None: ...


class GooglePlacesValidator:
    """Validates CandidatePlaces against Google Places in parallel."""

    def __init__(
        self,
        places_client: PlacesClient,
        confidence_config: ConfidenceConfig,
    ) -> None:
        self._places_client = places_client
        self._confidence_config = confidence_config

    async def validate(
        self, candidates: list[CandidatePlace]
    ) -> list[ValidatedCandidate] | None:
        if not candidates:
            return None

        raw = await asyncio.gather(
            *[self._validate_one(c) for c in candidates],
            return_exceptions=True,
        )
        results = [r for r in raw if isinstance(r, ValidatedCandidate)]
        return results if results else None

    async def _validate_one(
        self, candidate: CandidatePlace
    ) -> ValidatedCandidate | None:
        try:
            places_match: PlacesMatchResult = await self._places_client.validate_place(
                name=candidate.place_name,
                location=self._lookup_city(candidate),
            )
        except Exception:
            return None

        modifier = _QUALITY_MODIFIERS[places_match.match_quality]
        confidence = calculate_confidence(
            source=candidate.source,
            match_modifier=modifier,
            corroborated=candidate.corroborated,
            config=self._confidence_config,
            signals=candidate.signals or None,
        )

        if confidence == 0.0 or places_match.external_id is None:
            return None

        if _GEOGRAPHIC_PLACE_TYPES.intersection(places_match.place_types):
            return None

        provider = _PROVIDER_MAP.get(
            (places_match.external_provider or "").lower(), PlaceProvider.google
        )

        return ValidatedCandidate(
            place_name=places_match.validated_name or candidate.place_name,
            place_type=candidate.place_type,
            provider=provider,
            external_id=places_match.external_id,
            confidence=confidence,
            resolved_by=candidate.source,
            subcategory=candidate.subcategory,
            tags=candidate.tags,
            attributes=candidate.attributes,
            corroborated=candidate.corroborated,
            match_lat=places_match.lat,
            match_lng=places_match.lng,
            match_address=places_match.address,
        )

    @staticmethod
    def _lookup_city(candidate: CandidatePlace) -> str | None:
        """Read the city off the candidate's location_context for the provider call."""
        ctx = candidate.attributes.location_context
        return ctx.city if ctx is not None else None
