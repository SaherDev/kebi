"""Places module — the unified data layer for every place in the system."""

from kebi.core.places.models import (
    DuplicatePlaceError,
    DuplicateProviderId,
    GeoData,
    HoursDict,
    LocationContext,
    PlaceAttributes,
    PlaceCreate,
    PlaceEnrichment,
    PlaceObject,
    PlaceProvider,
    PlaceSource,
    PlaceType,
)
from kebi.core.places.places_client import (
    GooglePlacesClient,
    PlacesClient,
    PlacesMatchQuality,
    PlacesMatchResult,
)
from kebi.core.places.service import PlacesService

__all__ = [
    # New data layer (ADR-054 / feature 019)
    "PlacesService",
    "PlaceObject",
    "PlaceCreate",
    "PlaceType",
    "PlaceSource",
    "PlaceProvider",
    "PlaceAttributes",
    "LocationContext",
    "GeoData",
    "PlaceEnrichment",
    "HoursDict",
    "DuplicatePlaceError",
    "DuplicateProviderId",
    # Existing provider abstraction (ADR-049)
    "PlacesClient",
    "GooglePlacesClient",
    "PlacesMatchResult",
    "PlacesMatchQuality",
]
