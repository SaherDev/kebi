"""places — standalone places library.

Public surface: models, protocols, concrete implementations, services.
"""

from .area_geocoder import GoogleAreaGeocoder
from .cache import RedisPlacesCache
from .cached_embedder import CachedEmbedder
from .embedding_service import EmbeddingService
from .embeddings_repo import EMBEDDING_DIMENSIONS, EmbeddingsRepo
from .google_client import GooglePlacesClient
from .hybrid_search_repo import HybridSearchRepo
from .hybrid_search_service import HybridSearchService
from .models import (
    HybridSearchFilters,
    HybridSearchHit,
    LibrarySort,
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceObject,
    PlaceQuery,
    PlaceSource,
    PlaceTag,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
    UserPlaceStatusUpdate,
    normalize_icon,
)
from .nominatim_geocoding_client import (
    GeocodeResult,
    GeocodingError,
    NominatimGeocodingClient,
)
from .place_wipe_service import PlaceWipeService
from .places_repo import PlacesRepo
from .protocols import (
    EmbedderProtocol,
    EmbeddingServiceProtocol,
    EmbeddingsRepoProtocol,
    HybridSearchRepoProtocol,
    HybridSearchServiceProtocol,
    PlacesCacheProtocol,
    PlacesClientProtocol,
    PlacesRepoProtocol,
    PlacesSearchServiceProtocol,
    PlaceUpsertServiceProtocol,
    PlaceWipeServiceProtocol,
    UserPlacesRepoProtocol,
    UserPlacesServiceProtocol,
)
from .search_service import PlacesSearchService
from .tags import (
    AccessibilityTag,
    AtmosphereTag,
    CuisineTag,
    DietaryTag,
    FeatureTag,
    PriceTag,
    SeasonTag,
    ServiceTag,
    TagType,
    TagValue,
    TimeTag,
)
from .upsert_service import PlaceUpsertService
from .user_places_repo import UserPlacesRepo
from .user_places_service import (
    DuplicateUserPlaceError,
    PlaceNotFoundError,
    SaveLimitExceededError,
    UserPlacesService,
)

__all__ = [
    # tag vocabulary
    "TagType",
    "CuisineTag",
    "DietaryTag",
    "FeatureTag",
    "AtmosphereTag",
    "ServiceTag",
    "PriceTag",
    "AccessibilityTag",
    "TimeTag",
    "SeasonTag",
    "TagValue",
    # models
    "HybridSearchFilters",
    "HybridSearchHit",
    "LibrarySort",
    "LocationContext",
    "PlaceCategory",
    "PlaceCore",
    "PlaceNameAlias",
    "PlaceObject",
    "PlaceQuery",
    "PlaceSource",
    "PlaceTag",
    "SavedPlaceFilters",
    "SavedPlaceView",
    "UserPlace",
    "UserPlaceStatusUpdate",
    "normalize_icon",
    # protocols
    "EmbedderProtocol",
    "EmbeddingsRepoProtocol",
    "EmbeddingServiceProtocol",
    "HybridSearchRepoProtocol",
    "HybridSearchServiceProtocol",
    "PlacesCacheProtocol",
    "PlacesClientProtocol",
    "PlacesRepoProtocol",
    "PlacesSearchServiceProtocol",
    "PlaceUpsertServiceProtocol",
    "PlaceWipeServiceProtocol",
    "UserPlacesRepoProtocol",
    "UserPlacesServiceProtocol",
    # implementations
    "CachedEmbedder",
    "EmbeddingsRepo",
    "HybridSearchRepo",
    "PlacesRepo",
    "UserPlacesRepo",
    "RedisPlacesCache",
    "GoogleAreaGeocoder",
    "GooglePlacesClient",
    "NominatimGeocodingClient",
    # services
    "EmbeddingService",
    "HybridSearchService",
    "PlacesSearchService",
    "PlaceUpsertService",
    "PlaceWipeService",
    "UserPlacesService",
    # geocoding
    "GeocodeResult",
    # errors
    "DuplicateUserPlaceError",
    "PlaceNotFoundError",
    "SaveLimitExceededError",
    "GeocodingError",
    # constants
    "EMBEDDING_DIMENSIONS",
]
