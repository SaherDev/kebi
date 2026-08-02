"""Provider-agnostic geocoding abstraction.

`GeocoderProtocol` is the seam between the app and whichever geocoding
provider is wired in (Google Geocoding API today). Concrete adapters
live alongside in this package; a new provider is a new adapter class,
never an if/match branch. Verification policy (round-trip checks,
entity persistence) belongs to `core/areas`, not here.
"""

from __future__ import annotations

from kebi.providers.geocoding.cached import CachedGeocoder
from kebi.providers.geocoding.google import GoogleGeocoder
from kebi.providers.geocoding.null import NullGeocoder
from kebi.providers.geocoding.protocol import (
    GeocodeResult,
    GeocoderProtocol,
    GeocodingError,
)

__all__ = [
    "CachedGeocoder",
    "GeocodeResult",
    "GeocoderProtocol",
    "GeocodingError",
    "GoogleGeocoder",
    "NullGeocoder",
]
