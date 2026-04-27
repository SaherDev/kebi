"""places_v2 — standalone places library.

Public surface: models, protocols, concrete implementations, services.
"""

from .cache import RedisPlacesCache
from .cached_embedder import CachedEmbedder
from .embedding_service import EmbeddingService
from .embeddings_repo import EMBEDDING_DIMENSIONS, EmbeddingsRepo
from .google_client import GooglePlacesClient
from .hybrid_search_repo import HybridSearchRepo
from .hybrid_search_service import HybridSearchService
from .models import (
    HoursDict,
    HybridSearchFilters,
    HybridSearchHit,
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceObject,
    PlaceQuery,
    PlaceSource,
    PlaceTag,
    SavedPlaceView,
    UserPlace,
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
from .user_places_service import UserPlacesService

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
    "HoursDict",
    "HybridSearchFilters",
    "HybridSearchHit",
    "LocationContext",
    "PlaceCategory",
    "PlaceCore",
    "PlaceNameAlias",
    "PlaceObject",
    "PlaceQuery",
    "PlaceSource",
    "PlaceTag",
    "SavedPlaceView",
    "UserPlace",
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
    "GooglePlacesClient",
    # services
    "EmbeddingService",
    "HybridSearchService",
    "PlacesSearchService",
    "PlaceUpsertService",
    "PlaceWipeService",
    "UserPlacesService",
    # constants
    "EMBEDDING_DIMENSIONS",
]
