"""FastAPI dependencies for route handlers (ADR-019)."""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.agent.tools.candidate_namer import CandidateNamerService
from kebi.core.areas import AreaService
from kebi.core.areas.suggestion_service import AreaSuggestionService
from kebi.core.chat.consult_quota import ConsultQuotaService
from kebi.core.chat.service import ChatService
from kebi.core.config import AppConfig, ExtractionConfig, get_config, get_env
from kebi.core.events.dispatcher import EventDispatcher
from kebi.core.events.handlers import EventHandlers
from kebi.core.extraction.enrichment_level import EnrichmentLevel
from kebi.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    SearchServiceFactory,
    deep_summary,
    inline_summary,
)
from kebi.core.extraction.result_cache import ExtractionResultCache
from kebi.core.extraction.service import ExtractionService
from kebi.core.home import HomeService
from kebi.core.knowledge.curation_service import KnowledgeCurationService
from kebi.core.knowledge.curator import KnowledgeCurator
from kebi.core.knowledge.harvest_bucket import HarvestBucketReader, HarvestBucketWriter
from kebi.core.knowledge.harvester import KnowledgeHarvester
from kebi.core.knowledge.kebi_note import KebiNoteProducer
from kebi.core.knowledge.kebi_note_service import KebiNoteService
from kebi.core.knowledge.place_notes_service import PlaceNotesService
from kebi.core.knowledge.producer import KnowledgeIngestion
from kebi.core.knowledge.research_resolver import ResearchEntityResolver
from kebi.core.knowledge.research_service import (
    ResearchRankingWeights,
    ResearchService,
)
from kebi.core.knowledge.writer import KnowledgeWriter
from kebi.core.memory.buffer import MessageBuffer
from kebi.core.memory.extractor import MemoryExtractor
from kebi.core.memory.repository import SQLAlchemyUserMemoryRepository
from kebi.core.memory.service import UserMemoryService
from kebi.core.places import (
    CachedEmbedder,
    EmbeddingService,
    EmbeddingsRepo,
    GooglePlacesClient,
    HybridSearchRepo,
    HybridSearchService,
    PlacesRepo,
    PlacesSearchService,
    PlaceUpsertService,
    RedisPlacesCache,
    UserPlacesRepo,
    UserPlacesService,
)
from kebi.core.signal.service import SignalService
from kebi.core.taste.debounce import regen_debouncer
from kebi.core.taste.service import TasteModelService
from kebi.core.user.intent_service import UserIntentService
from kebi.core.user.service import UserDataDeletionService
from kebi.db.repositories import (
    AreaEntityRepository,
    KnowledgeClaimRepository,
    SQLAlchemyAreaEntityRepository,
    SQLAlchemyKnowledgeClaimRepository,
)
from kebi.db.repositories.user_intent_repository import (
    SQLAlchemyUserIntentRepository,
)
from kebi.db.session import _get_session_factory, get_session
from kebi.providers import get_instructor_client
from kebi.providers.cache import CacheBackend
from kebi.providers.embeddings import EmbedderProtocol, get_embedder
from kebi.providers.geocoding import (
    CachedGeocoder,
    GeocoderProtocol,
    GoogleGeocoder,
    NullGeocoder,
)
from kebi.providers.http_client import get_shared_http_client
from kebi.providers.llm import get_transcription_client, get_vision_extractor
from kebi.providers.object_storage import (
    NullObjectStorage,
    ObjectStorageProtocol,
    S3ObjectStorage,
)
from kebi.providers.redis_cache import RedisCacheBackend, get_redis_client

# ---------------------------------------------------------------------------
# Gateway service-to-service auth.
#
# kebi exposes HTTP routes but never authenticates end users itself. The
# NestJS gateway holds GATEWAY_SHARED_SECRET, verifies the Clerk session
# on its side, and forwards two headers on every protected call:
#
#   X-Gateway-Token    — the shared secret (constant-time compared)
#   X-Gateway-User-Id  — the verified Clerk subject (e.g. "user_2abc...")
#
# Both headers are required; a missing or wrong token is 401, a malformed
# user id is 400. user_id format is restricted to the Clerk pattern so it
# can be safely substituted into Redis keys, checkpointer thread ids, and
# parameterised SQL without collateral injection paths.
# ---------------------------------------------------------------------------

_USER_ID_PATTERN = re.compile(r"^user_[A-Za-z0-9]{20,40}$")


@dataclass(frozen=True)
class GatewayIdentity:
    """A verified caller identity forwarded by the NestJS gateway.

    Constructed only inside `require_gateway_identity` after the shared
    secret check passes. Routes treat the `user_id` field as ground truth
    — body-level / path-level `user_id` is no longer accepted.

    Plan-tier entitlements travel on the same trusted header channel as the
    identity (the gateway owns plans; kebi enforces). They are gateway-
    asserted facts about the caller — never read from the request body,
    which the end-user could forge. kebi receives raw capabilities, never
    the plan name, so repricing never touches this repo.

    Defaults are restrictive: booleans default `False` (fail closed — a
    missing header denies the paid feature); int limits default `None`
    (= unlimited), because kebi must stay pricing-agnostic and cannot
    invent the free-tier numbers. The `None`-means-unlimited choice fails
    *open* for `consults_per_day` — the slowapi per-minute limit is the
    only backstop if the gateway omits it.
    """

    user_id: str
    taste_enabled: bool = False
    discovery_enabled: bool = False
    save_limit: int | None = None
    consults_per_day: int | None = None
    advanced_models_enabled: bool = False
    # Whether this caller may push curated_expert knowledge (ADR-121). A
    # global write, so it fails closed like the other boolean gates.
    can_curate: bool = False


def require_gateway_identity(
    request: Request,
    x_gateway_token: str = Header(..., alias="X-Gateway-Token"),
    x_gateway_user_id: str = Header(..., alias="X-Gateway-User-Id"),
    x_gateway_taste_enabled: bool = Header(False, alias="X-Gateway-Taste-Enabled"),
    x_gateway_discovery_enabled: bool = Header(
        False, alias="X-Gateway-Discovery-Enabled"
    ),
    x_gateway_save_limit: int | None = Header(None, alias="X-Gateway-Save-Limit"),
    x_gateway_consults_per_day: int | None = Header(
        None, alias="X-Gateway-Consults-Per-Day"
    ),
    x_gateway_advanced_models_enabled: bool = Header(
        False, alias="X-Gateway-Advanced-Models-Enabled"
    ),
    x_gateway_can_curate: bool = Header(False, alias="X-Gateway-Can-Curate"),
) -> GatewayIdentity:
    """Verify the gateway shared secret and return the forwarded identity.

    Mounted as a global dependency on the protected router. The public
    router (just `/v1/health`) bypasses this — health probes must remain
    reachable for load-balancer / Railway HEALTHCHECK.
    """
    expected = get_env().GATEWAY_SHARED_SECRET
    if not expected:
        # Misconfigured deploy — fail closed.
        raise HTTPException(status_code=503, detail="auth_misconfigured")
    if not secrets.compare_digest(x_gateway_token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not _USER_ID_PATTERN.match(x_gateway_user_id):
        raise HTTPException(status_code=400, detail="bad_user_id")
    identity = GatewayIdentity(
        user_id=x_gateway_user_id,
        taste_enabled=x_gateway_taste_enabled,
        discovery_enabled=x_gateway_discovery_enabled,
        save_limit=x_gateway_save_limit,
        consults_per_day=x_gateway_consults_per_day,
        advanced_models_enabled=x_gateway_advanced_models_enabled,
        can_curate=x_gateway_can_curate,
    )
    # Stash on request.state so middleware (rate limiter, request-id
    # logger) can reach it without re-resolving the dep chain.
    request.state.identity = identity
    return identity


def get_taste_service() -> TasteModelService:
    """FastAPI dependency providing TasteModelService.

    Uses session_factory so each repo method opens its own session. The
    place-resolution dependencies are session-scoped factories built per
    regen inside the service's own short-lived DB scope (ADR-077
    analytical read; ADR-072: no new long-lived shared dependency).
    `_build_taste_place_resolver` is defined in the places deps
    section below and resolved at call time.
    """
    return TasteModelService(
        session_factory=_get_session_factory(),
        search_service_factory=_build_taste_place_resolver,
        user_places_repo_factory=UserPlacesRepo,
    )


def get_cache_backend() -> CacheBackend:
    """FastAPI dependency providing CacheBackend (RedisCacheBackend by default)."""
    return RedisCacheBackend(client=get_redis_client(get_env().REDIS_URL))


def _build_message_buffer() -> MessageBuffer:
    """Construct a per-user message buffer backed by the shared Redis client.

    The underlying `redis.asyncio.Redis` is a process-wide singleton owned
    by `providers/redis_cache.get_redis_client`, so the connection pool is
    reused across requests (ADR-019).
    """
    cfg = get_config()
    return MessageBuffer(
        redis=get_redis_client(get_env().REDIS_URL),
        ttl_seconds=cfg.memory.extraction.buffer_ttl_seconds,
    )


def get_user_intent_service() -> UserIntentService:
    """FastAPI dependency providing UserIntentService (ADR-110).

    Repo uses session_factory — each method opens its own session, so the
    service is safe in both the request path (GET /v1/user/intents) and the
    background turn-completed handler (intent persistence).
    """
    return UserIntentService(
        repo=SQLAlchemyUserIntentRepository(_get_session_factory()),
        config=get_config().user_intents,
    )


def get_user_memory_service() -> UserMemoryService:
    """FastAPI dependency providing UserMemoryService.

    CRITICAL (ADR-038): SQLAlchemyUserMemoryRepository is constructed ONLY here.
    Repo uses session_factory — each method opens its own session.
    """
    cfg = get_config()
    return UserMemoryService(
        repo=SQLAlchemyUserMemoryRepository(_get_session_factory()),
        extractor=MemoryExtractor(get_instructor_client("memory_extractor")),
        confidence_config=cfg.memory.confidence,
        buffer=_build_message_buffer(),
        debounce_messages=cfg.memory.extraction.debounce_messages,
    )


def get_extraction_config(
    config: AppConfig = Depends(get_config),  # noqa: B008
) -> ExtractionConfig:
    """FastAPI dependency providing ExtractionConfig."""
    return config.extraction


async def get_event_dispatcher(
    background_tasks: BackgroundTasks,
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
    memory_service: UserMemoryService = Depends(get_user_memory_service),  # noqa: B008
    intent_service: UserIntentService = Depends(get_user_intent_service),  # noqa: B008
) -> EventDispatcher:
    """FastAPI dependency providing a fully wired EventDispatcher (ADR-043).

    Pulls `taste_service`, `memory_service`, and `intent_service` from the
    existing `Depends(...)` factories so FastAPI's per-request dedup hands
    out the same instances the rest of the request graph already uses
    (ADR-019). All use session_factory internally — each repo method opens
    its own session, so background tasks don't depend on request session.
    The harvest stack backing `content_harvest_requested` (ADR-121) needs no
    request scope, so it is built from its process-wide providers here rather
    than injected. `BackgroundTasks` stays request-scoped (FastAPI req).
    """
    handlers = EventHandlers(
        taste_service=taste_service,
        memory_service=memory_service,
        intent_service=intent_service,
        harvest_reader=get_harvest_bucket_reader(get_object_storage()),
        harvester=get_knowledge_harvester(),
        ingestion=get_knowledge_ingestion(
            get_knowledge_writer(get_knowledge_claim_repository())
        ),
    )

    dispatcher = EventDispatcher(background_tasks=background_tasks)
    for event_type in (
        "place_saved",
        "recommendation_accepted",
        "recommendation_rejected",
        "recommendation_saved",
    ):
        dispatcher.register_handler(event_type, handlers.on_taste_signal)
    dispatcher.register_handler(
        "library_state_changed",
        handlers.on_library_state_changed,  # type: ignore[arg-type]
    )
    dispatcher.register_handler(
        "turn_completed",
        handlers.on_turn_completed,  # type: ignore[arg-type]
    )
    dispatcher.register_handler(
        "content_harvest_requested",
        handlers.on_content_harvest_requested,  # type: ignore[arg-type]
    )
    dispatcher.register_handler(
        "area_interest_noted",
        handlers.on_area_interest_noted,  # type: ignore[arg-type]
    )

    return dispatcher


def _make_inline_level() -> EnrichmentLevel:
    """Build the inline enrichment level with singleton circuit breakers.

    Enrichers are pure caption/text producers. NER lives at the
    pipeline as the shared finalizer — runs after every executed level.

    `SubtitleCheckEnricher` lives here (not in the deep level): it is a
    cheap text producer (yt-dlp `--skip-download`), so harvesting
    subtitles inline lets a subtitled video resolve and short-circuit
    before the expensive deep level (Whisper + vision) runs. It
    self-guards on no-URL / photo-post, and `WhisperAudioEnricher`
    still early-returns when `context.transcript` is already set
    (context persists across levels), so the deep-level Whisper is
    still skipped when inline subtitles were found.
    """
    from kebi.core.extraction.circuit_breaker import (
        CircuitBreakerEnricher,
        ParallelEnricherGroup,
    )
    from kebi.core.extraction.enrichers.google_maps_list import (
        GoogleMapsListEnricher,
    )
    from kebi.core.extraction.enrichers.instagram_post import (
        InstagramPostEnricher,
    )
    from kebi.core.extraction.enrichers.subtitle_check import (
        SubtitleCheckEnricher,
    )
    from kebi.core.extraction.enrichers.tiktok_caption import (
        TikTokCaptionEnricher,
    )
    from kebi.core.extraction.enrichers.tiktok_photo import (
        TikTokPhotoEnricher,
    )
    from kebi.core.extraction.enrichers.video_metadata import (
        VideoMetadataEnricher,
    )

    http = get_shared_http_client()
    return EnrichmentLevel(
        name="enrich",
        enrichers=[
            ParallelEnricherGroup(
                [
                    CircuitBreakerEnricher(TikTokCaptionEnricher(http=http)),
                    CircuitBreakerEnricher(VideoMetadataEnricher()),
                    CircuitBreakerEnricher(GoogleMapsListEnricher(http=http)),
                    CircuitBreakerEnricher(InstagramPostEnricher(http=http)),
                    CircuitBreakerEnricher(TikTokPhotoEnricher(http=http)),
                    CircuitBreakerEnricher(SubtitleCheckEnricher()),
                ]
            ),
        ],
        summary_fn=inline_summary,
    )


# Module-level singleton so circuit breaker state persists across requests.
_inline_level: EnrichmentLevel | None = None


def _get_inline_level() -> EnrichmentLevel:
    global _inline_level
    if _inline_level is None:
        _inline_level = _make_inline_level()
    return _inline_level


def _make_deep_level() -> EnrichmentLevel:
    """Build the URL-only deep enrichment level (audio/vision).

    Whisper is a pure text producer — it populates `context.transcript`
    (and early-returns when subtitles already set it at the inline
    level). Vision goes image → place names directly via a vision LLM
    (no text intermediate). Subtitle harvesting moved to the inline
    level — it is cheap and lets subtitled videos resolve without
    paying for this level. NER lives at the pipeline as the shared
    finalizer — runs after this level, sees the just-populated
    transcript alongside any caption / supplementary text.
    """
    from kebi.core.extraction.enrichers.vision_frames import VisionFramesEnricher
    from kebi.core.extraction.enrichers.vision_images import VisionImagesEnricher
    from kebi.core.extraction.enrichers.whisper_audio import WhisperAudioEnricher

    vision_extractor = get_vision_extractor()
    extraction_cfg = get_config().extraction
    return EnrichmentLevel(
        name="deep_enrichment",
        enrichers=[
            WhisperAudioEnricher(
                transcription_client=get_transcription_client(),
                config=extraction_cfg.whisper,
            ),
            VisionFramesEnricher(
                vision_extractor=vision_extractor,
                config=extraction_cfg.vision,
            ),
            VisionImagesEnricher(
                vision_extractor=vision_extractor,
                http=get_shared_http_client(),
            ),
        ],
        summary_fn=deep_summary,
        requires_url=True,
    )


# Cached so the underlying instructor/transcription/vision clients are
# built once at process start, not rebuilt per request.
_deep_level: EnrichmentLevel | None = None


def _get_deep_level() -> EnrichmentLevel:
    global _deep_level
    if _deep_level is None:
        _deep_level = _make_deep_level()
    return _deep_level


# get_extraction_pipeline and get_extraction_service are defined below
# the places deps section so their PlacesSearchService /
# PlaceUpsertService / UserPlacesService / UserPlacesRepo factories
# (defined there) are already in scope when FastAPI resolves the
# default-value Depends() at module load time.


def get_signal_service(
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
) -> SignalService:
    """FastAPI dependency providing SignalService (ADR-060, ADR-078).

    Recommendation accept/reject signals are no longer DB-validated — the
    recommendations table was dropped (ADR-078); the signal is trusted from
    the product repo and dispatched as an event.

    An **area** save takes a different path and so needs two more
    collaborators: the shared `AreaService` reads what the supplied key
    actually is, and `TasteModelService` records the region interest
    directly. There is no event because there is no row to write — an area
    save trains taste and stops there.
    """
    return SignalService(
        event_dispatcher=event_dispatcher,
        areas=get_area_service(),
        taste_service=get_taste_service(),
    )


def get_agent_checkpointer(request: Request) -> Any:
    """Return the process-scoped `AsyncPostgresSaver` warmed at startup.

    Populated by `api/main.py::_warm_agent_checkpointer`. Returns `None`
    when the lifespan hasn't run (test clients) or warmup failed.
    """
    return getattr(request.app.state, "agent_checkpointer", None)


def get_user_data_deletion_service(
    checkpointer: Any = Depends(get_agent_checkpointer),  # noqa: B008
) -> UserDataDeletionService:
    """FastAPI dependency providing UserDataDeletionService.

    Sweeps the five user-scoped tables in one transaction (interactions,
    user_memories, taste_model, user_intents, user_places), then deletes the
    LangGraph checkpoint thread, then cancels any pending taste-regen task. The
    shared places/embeddings catalog is intentionally untouched (it is
    cross-user, not this user's data). Erases AI-owned data only — NestJS
    is responsible for deleting the user account itself. Hard-delete
    only, sync sweep, 204 No Content.
    """
    return UserDataDeletionService(
        session_factory=_get_session_factory(),
        checkpointer=checkpointer,
        regen_debouncer=regen_debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )


# get_agent_graph and get_chat_service are defined at the bottom of this
# file because they consume `get_extraction_service`, which in turn
# consumes the places factories declared further down.


# ---------------------------------------------------------------------------
# places dependencies
# ---------------------------------------------------------------------------


def get_places_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlacesRepo:
    """FastAPI dependency providing PlacesRepo (places table)."""
    return PlacesRepo(db_session)


def get_user_places_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> UserPlacesRepo:
    """FastAPI dependency providing UserPlacesRepo (user_places table)."""
    return UserPlacesRepo(db_session)


def get_places_cache() -> RedisPlacesCache:
    """FastAPI dependency providing RedisPlacesCache (place: key prefix).

    Backed by the process-wide Redis client from `providers/redis_cache`.
    """
    return RedisPlacesCache(redis=get_redis_client(get_env().REDIS_URL))


def get_google_places_client() -> GooglePlacesClient:
    """FastAPI dependency providing GooglePlacesClient (places).

    Backed by the process-wide httpx.AsyncClient from `providers/http_client`.
    """
    return GooglePlacesClient(
        api_key=get_env().GOOGLE_API_KEY or "",
        http=get_shared_http_client(),
    )


def get_geocoder() -> GeocoderProtocol:
    """FastAPI dependency providing the geocoding boundary (area layer).

    GoogleGeocoder over the Geocoding API (forward + reverse + place-id
    refresh), with reverse lookups behind the Redis bucket cache. Falls
    back to the NullGeocoder when no GOOGLE_API_KEY is set so dev/test
    environments still boot; every lookup then resolves to no-match and
    callers degrade (clarify / fail open).
    """
    cfg = get_config().geocoding
    api_key = get_env().GOOGLE_API_KEY
    if not api_key:
        return NullGeocoder()
    return CachedGeocoder(
        GoogleGeocoder(
            api_key=api_key,
            http=get_shared_http_client(),
            timeout_seconds=cfg.timeout_seconds,
        ),
        get_redis_client(get_env().REDIS_URL),
        ttl_seconds=cfg.reverse_cache.ttl_seconds,
        precision=cfg.reverse_cache.precision,
    )


def get_embeddings_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EmbeddingsRepo:
    """FastAPI dependency providing EmbeddingsRepo (place_embeddings)."""
    return EmbeddingsRepo(db_session)


def get_places_embedder() -> EmbedderProtocol:
    """Single embedder used by both the document path (PlaceUpsertService
    → EmbeddingService) and the query path (HybridSearchService).

    CachedEmbedder wraps the configured embedder with Redis (90-day TTL,
    SHA-256 key over `model_name | input_type | normalized_text`). The
    `input_type` segment keeps document and query vectors in separate
    cache slots so Voyage's asymmetric embedding is preserved.

    `model_name` lives in the key, so a model swap automatically
    invalidates without manual cleanup.
    """
    return CachedEmbedder(
        embedder=get_embedder(),
        redis=get_redis_client(get_env().REDIS_URL),
        model_name=get_config().models["embedder"].model,
    )


def get_embedding_service(
    repo: EmbeddingsRepo = Depends(get_embeddings_repo),  # noqa: B008
    embedder: EmbedderProtocol = Depends(get_places_embedder),  # noqa: B008
    config: AppConfig = Depends(get_config),  # noqa: B008
) -> EmbeddingService:
    """FastAPI dependency providing EmbeddingService (places documents).

    Uses the same `get_places_embedder` (Redis-backed CachedEmbedder) that
    the query path uses. The cache key includes `input_type`, so
    document and query vectors don't collide. `EmbeddingService`
    runs its diff-then-embed `(text_hash, model_name)` check first,
    so the cache only sees texts that actually need embedding —
    cheap Redis round-trip, with a real hit when re-extracting the
    same venue from different posts.
    """
    return EmbeddingService(
        repo=repo,
        embedder=embedder,
        model_name=config.models["embedder"].model,
    )


def get_place_upsert_service(
    repo: PlacesRepo = Depends(get_places_repo),  # noqa: B008
    embedding_service: EmbeddingService = Depends(  # noqa: B008
        get_embedding_service
    ),
) -> PlaceUpsertService:
    """FastAPI dependency providing PlaceUpsertService (places)."""
    return PlaceUpsertService(repo=repo, embedding_service=embedding_service)


def get_places_search_service(
    repo: PlacesRepo = Depends(get_places_repo),  # noqa: B008
    cache: RedisPlacesCache = Depends(get_places_cache),  # noqa: B008
    client: GooglePlacesClient = Depends(get_google_places_client),  # noqa: B008
    upsert_service: PlaceUpsertService = Depends(  # noqa: B008
        get_place_upsert_service
    ),
) -> PlacesSearchService:
    """FastAPI dependency providing PlacesSearchService (places)."""
    return PlacesSearchService(
        repo=repo,
        cache=cache,
        client=client,
        upsert_service=upsert_service,
    )


def _build_taste_place_resolver(session: AsyncSession) -> PlacesSearchService:
    """Background-safe PlacesSearchService bound to one explicit session.

    Reuses the exact request-path factories (`get_places_search_service`
    and its transitive `get_place_upsert_service` / `get_embedding_service`)
    — the only difference is the DB session is supplied explicitly instead
    of resolved by FastAPI's `Depends(get_session)`, because taste regen
    runs in a background debounced task with no request scope. Those
    factories are plain functions; their `Depends()` defaults only apply
    when FastAPI resolves them, so calling with explicit args is the
    documented reuse path. No construction logic is duplicated.

    Used only by TasteModelService's analytical read (ADR-077): it calls
    `get_cores_by_ids`, which is DB-only — cache/client/upsert satisfy the
    constructor contract but are never exercised there. Factory
    construction stays in the wiring layer (ADR-072); the consumer
    receives this callable via injection.
    """
    repo = PlacesRepo(session)
    return get_places_search_service(
        repo=repo,
        cache=get_places_cache(),
        client=get_google_places_client(),
        upsert_service=get_place_upsert_service(
            repo=repo,
            embedding_service=get_embedding_service(
                repo=EmbeddingsRepo(session),
                embedder=get_places_embedder(),
                config=get_config(),
            ),
        ),
    )


def get_search_service_factory(
    cache: RedisPlacesCache = Depends(get_places_cache),  # noqa: B008
    client: GooglePlacesClient = Depends(get_google_places_client),  # noqa: B008
) -> SearchServiceFactory:
    """Per-task PlacesSearchService factory for the extraction fan-out
    (ADR-070, ADR-072).

    `ExtractionPipeline._extend_search_set` issues N `find()` calls
    concurrently under `asyncio.gather`. A SQLAlchemy `AsyncSession`
    is not concurrency-safe, so each call must use its own session.
    Each `async with factory()` opens a fresh session, builds the
    DB-bound repos against it, and yields a `PlacesSearchService`
    reusing the exact request-path factories (mirrors
    `_build_taste_place_resolver` — no construction logic duplicated).
    Cache + Google client are process-safe and shared across tasks;
    only the session/repos are per-task.

    Pool sizing: the fan-out is bounded by the pipeline's search
    semaphore (`_SEARCH_CONCURRENCY = 5`), so peak open sessions per
    in-flight extraction is 1 (request) + 5 (fan-out) = 6, well under
    the async engine's default 15 (pool_size 5 + overflow 10). Each
    `async with` closes its session promptly. Raising
    `_SEARCH_CONCURRENCY` requires revisiting pool size.
    """

    @asynccontextmanager
    async def _factory() -> AsyncIterator[PlacesSearchService]:
        async with _get_session_factory()() as session:
            repo = PlacesRepo(session)
            yield get_places_search_service(
                repo=repo,
                cache=cache,
                client=client,
                upsert_service=get_place_upsert_service(
                    repo=repo,
                    embedding_service=get_embedding_service(
                        repo=EmbeddingsRepo(session),
                        embedder=get_places_embedder(),
                        config=get_config(),
                    ),
                ),
            )

    return _factory


def get_user_places_service(
    user_places_repo: UserPlacesRepo = Depends(get_user_places_repo),  # noqa: B008
) -> UserPlacesService:
    """FastAPI dependency providing UserPlacesService (places)."""
    return UserPlacesService(user_places_repo=user_places_repo)


# ---------------------------------------------------------------------------
# places — hybrid search
# ---------------------------------------------------------------------------


def get_hybrid_search_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> HybridSearchRepo:
    """FastAPI dependency providing HybridSearchRepo (places)."""
    return HybridSearchRepo(db_session)


def get_hybrid_search_service(
    repo: HybridSearchRepo = Depends(get_hybrid_search_repo),  # noqa: B008
    embedder: EmbedderProtocol = Depends(get_places_embedder),  # noqa: B008
) -> HybridSearchService:
    """FastAPI dependency providing HybridSearchService (places).

    Shares `get_places_embedder` with the document-side EmbeddingService.
    CachedEmbedder keys by `input_type`, so query vectors don't
    collide with document vectors even though both paths hit the same
    cache.
    """
    return HybridSearchService(repo=repo, embedder=embedder)


# ---------------------------------------------------------------------------
# Extraction pipeline + service (lives after places deps because it
# depends on PlacesSearchService / PlaceUpsertService / UserPlacesService /
# UserPlacesRepo factories defined above).
# ---------------------------------------------------------------------------


def get_extraction_pipeline(
    extraction_config: ExtractionConfig = Depends(get_extraction_config),  # noqa: B008
    search_service: PlacesSearchService = Depends(  # noqa: B008
        get_places_search_service
    ),
    search_service_factory: SearchServiceFactory = Depends(  # noqa: B008
        get_search_service_factory
    ),
) -> ExtractionPipeline:
    """FastAPI dependency providing ExtractionPipeline with all levels wired.

    Per ADR-070, the search step delegates to
    `places.PlacesSearchService` (DB-first lookup with cache
    overlay, provider fallback, upsert). Extraction never calls Google
    directly anymore.
    """
    from kebi.core.extraction.enrichers.llm_picker import LLMPlacePicker
    from kebi.core.extraction.enrichers.llm_resolver import LLMResolver

    return ExtractionPipeline(
        levels=[_get_inline_level(), _get_deep_level()],
        search_service=search_service,
        search_service_factory=search_service_factory,
        resolver=LLMResolver(
            instructor_client=get_instructor_client("extractor"),
        ),
        picker=LLMPlacePicker(
            instructor_client=get_instructor_client("extractor"),
            confidence_config=extraction_config.confidence,
        ),
        extraction_config=extraction_config,
    )


# Process-wide singleton: the aioboto3 Session caches credentials and is
# safe to reuse across requests. Built lazily so unset env vars short-circuit
# to NullObjectStorage without trying to instantiate boto3.
_object_storage: ObjectStorageProtocol | None = None


def _build_object_storage() -> ObjectStorageProtocol:
    env = get_env()
    if not (
        env.BUCKET_NAME and env.BUCKET_ACCESS_KEY_ID and env.BUCKET_SECRET_ACCESS_KEY
    ):
        return NullObjectStorage()
    return S3ObjectStorage(
        bucket=env.BUCKET_NAME,
        endpoint_url=env.BUCKET_ENDPOINT_URL,
        access_key_id=env.BUCKET_ACCESS_KEY_ID,
        secret_access_key=env.BUCKET_SECRET_ACCESS_KEY,
        region=env.BUCKET_REGION,
    )


def get_object_storage() -> ObjectStorageProtocol:
    """Return the process-wide ObjectStorageProtocol implementation.

    Returns `S3ObjectStorage` (Railway / AWS / R2 / MinIO — anything
    speaking the S3 wire protocol) when bucket env vars are set;
    otherwise `NullObjectStorage` so local dev runs without a real
    bucket. Swapping providers is endpoint-URL-only — no code change.
    """
    global _object_storage
    if _object_storage is None:
        _object_storage = _build_object_storage()
    return _object_storage


def get_harvest_bucket_writer(
    storage: ObjectStorageProtocol = Depends(get_object_storage),  # noqa: B008
) -> HarvestBucketWriter:
    """FastAPI dependency providing the HarvestBucketWriter (ADR-121).

    Snapshots a share's already-gathered content to object storage so the
    background harvest pass can mine it without re-fetching. Storage is
    pluggable via `ObjectStorageProtocol`; the write is non-fatal.
    """
    return HarvestBucketWriter(storage=storage)


def get_harvest_bucket_reader(
    storage: ObjectStorageProtocol = Depends(get_object_storage),  # noqa: B008
) -> HarvestBucketReader:
    """FastAPI dependency providing the HarvestBucketReader (ADR-121).

    Reads a harvest snapshot back for the background handler.
    """
    return HarvestBucketReader(storage=storage)


def get_area_entity_repository() -> AreaEntityRepository:
    """FastAPI dependency providing the AreaEntityRepository (Step 2).

    Uses session_factory — each method opens its own session, so it is safe
    in both the request path and the background harvest handler.
    """
    return SQLAlchemyAreaEntityRepository(_get_session_factory())


def get_area_service() -> AreaService:
    """FastAPI dependency providing the AreaService — the only area
    resolution path: store first, geocode on miss, verified or refused.
    A fresh instance per call keeps the memo request-scoped."""
    return AreaService(get_area_entity_repository(), get_geocoder())


def get_knowledge_claim_repository() -> KnowledgeClaimRepository:
    """FastAPI dependency providing the KnowledgeClaimRepository (ADR-120).

    Uses session_factory — each method opens its own session, so it is safe
    in both the request path (curator) and the background harvest handler.
    """
    return SQLAlchemyKnowledgeClaimRepository(_get_session_factory())


def get_knowledge_writer(
    repo: KnowledgeClaimRepository = Depends(  # noqa: B008
        get_knowledge_claim_repository
    ),
) -> KnowledgeWriter:
    """FastAPI dependency providing the shared KnowledgeWriter (ADR-121).

    Mechanical write path; provenance is supplied by the producer via
    `KnowledgeIngestion`.
    """
    return KnowledgeWriter(repo=repo)


def get_knowledge_ingestion(
    writer: KnowledgeWriter = Depends(get_knowledge_writer),  # noqa: B008
) -> KnowledgeIngestion:
    """FastAPI dependency providing the source-agnostic KnowledgeIngestion
    (ADR-122): persists any ClaimProducer's claims under its own provenance."""
    return KnowledgeIngestion(writer)


def get_knowledge_harvester() -> KnowledgeHarvester:
    """FastAPI dependency providing the KnowledgeHarvester (ADR-121/122).

    A `shared_content` ClaimProducer; its trust floor and review status come
    from config, so gating harvested claims later is a config change. Claims
    naming an entity other than their anchor place are re-keyed through the
    shared AreaService — store first, round-trip-verified geocode on miss
    (ADR-126) — persisting the named area as an entity as a side effect.
    """
    knowledge = get_config().knowledge
    return KnowledgeHarvester(
        get_instructor_client("knowledge_harvester"),
        get_area_service(),
        confidence_floor=knowledge.harvest_confidence_floor,
        review_status=knowledge.harvest_review_status,
    )


def get_knowledge_curator() -> KnowledgeCurator:
    """FastAPI dependency providing the KnowledgeCurator (ADR-121/122).

    A `curated_expert` ClaimProducer; resolves each claim's structured area
    components through the shared AreaService so a curated claim keys
    identically to a harvested one. Trust floor and review status come from
    config.
    """
    knowledge = get_config().knowledge
    return KnowledgeCurator(
        get_instructor_client("knowledge_curator"),
        get_area_service(),
        confidence_floor=knowledge.curator_confidence_floor,
        review_status=knowledge.curator_review_status,
    )


def get_knowledge_curation_service(
    ingestion: KnowledgeIngestion = Depends(get_knowledge_ingestion),  # noqa: B008
) -> KnowledgeCurationService:
    """FastAPI dependency providing the KnowledgeCurationService (ADR-121)."""
    return KnowledgeCurationService(
        curator=get_knowledge_curator(),
        ingestion=ingestion,
    )


def get_kebi_note_producer() -> KebiNoteProducer:
    """FastAPI dependency providing the KebiNoteProducer (ADR-127).

    A `kebi_message` ClaimProducer — turns a saved-recommendation reason into a
    place-scoped claim. Trust floor and review status come from config, so
    gating these notes later is a config change (no LLM, no geocoder).
    """
    knowledge = get_config().knowledge
    return KebiNoteProducer(
        confidence_floor=knowledge.kebi_message_confidence_floor,
        review_status=knowledge.kebi_message_review_status,
    )


def get_kebi_note_service(
    ingestion: KnowledgeIngestion = Depends(get_knowledge_ingestion),  # noqa: B008
) -> KebiNoteService:
    """FastAPI dependency providing the KebiNoteService (ADR-127).

    Records a saved-recommendation reason as a user-scoped `kebi_message`
    claim, through the same source-agnostic ingestion seam the curator uses.
    """
    return KebiNoteService(
        producer=get_kebi_note_producer(),
        ingestion=ingestion,
    )


def get_place_notes_service(
    repo: KnowledgeClaimRepository = Depends(  # noqa: B008
        get_knowledge_claim_repository
    ),
) -> PlaceNotesService:
    """FastAPI dependency providing the PlaceNotesService (ADR-127).

    The knowledge layer's first reader — surfaces the claims tied to a saved
    place as insider notes on the Library. `place_notes_limit` (config) caps
    how many notes surface on one place.
    """
    return PlaceNotesService(repo, limit=get_config().knowledge.place_notes_limit)


def get_research_service(
    repo: KnowledgeClaimRepository = Depends(  # noqa: B008
        get_knowledge_claim_repository
    ),
) -> ResearchService:
    """FastAPI dependency providing the ResearchService.

    The knowledge layer's agent-facing reader behind the `research` tool:
    staged verified-or-refuse entity resolution over the AreaService
    (store first, geocode on miss), then an entity-bounded, approved-only
    claims read ranked in memory. Limits, weights, and thresholds come
    from config (`agent.research`, `knowledge.research`).
    """
    cfg = get_config()
    research_cfg = cfg.knowledge.research
    resolver = ResearchEntityResolver(
        get_area_service(),
        confidence_min=research_cfg.entity_confidence_min,
    )
    return ResearchService(
        repo,
        resolver,
        default_limit=cfg.agent.research.default_limit,
        max_limit=cfg.agent.research.max_limit,
        notes_limit=cfg.agent.research.notes_limit,
        weights=ResearchRankingWeights(
            w_tag=research_cfg.w_tag,
            w_text=research_cfg.w_text,
            w_trust=research_cfg.w_trust,
            w_prox=research_cfg.w_prox,
        ),
        topic_relevance_floor=research_cfg.topic_relevance_floor,
    )


def get_area_suggestion_service(
    repo: KnowledgeClaimRepository = Depends(  # noqa: B008
        get_knowledge_claim_repository
    ),
) -> AreaSuggestionService:
    """FastAPI dependency providing the AreaSuggestionService.

    Behind the `suggest_areas` tool (location-kinds Step 6): resolves the
    areas the agent named through the shared `AreaService` — the one area
    authority, store first with a round-trip-verified geocode on miss — and
    attaches their claims from the same repository `research` reads.
    """
    cfg = get_config().agent.suggest_areas
    return AreaSuggestionService(
        get_area_service(),
        repo,
        max_names=cfg.max_names,
        notes_limit=cfg.notes_limit,
    )


def get_extraction_result_cache(
    config: AppConfig = Depends(get_config),  # noqa: B008
) -> ExtractionResultCache:
    """FastAPI dependency providing ExtractionResultCache (ADR-074).

    Backed by the process-wide Redis client from `providers/redis_cache`.
    TTL is sourced from `config.extraction.result_cache_ttl_seconds`
    (30-day default per `app.yaml`). Per ADR-072 (SSP), the factory
    call is contained to this wiring layer; `ExtractionService`
    receives the constructed cache via constructor injection and never
    imports the factory itself.
    """
    return ExtractionResultCache(
        redis=get_redis_client(get_env().REDIS_URL),
        ttl_seconds=config.extraction.result_cache_ttl_seconds,
    )


def get_extraction_service(
    pipeline: ExtractionPipeline = Depends(get_extraction_pipeline),  # noqa: B008
    upsert_service: PlaceUpsertService = Depends(  # noqa: B008
        get_place_upsert_service
    ),
    user_places_service: UserPlacesService = Depends(  # noqa: B008
        get_user_places_service
    ),
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
    result_cache: ExtractionResultCache = Depends(  # noqa: B008
        get_extraction_result_cache
    ),
    harvest_writer: HarvestBucketWriter = Depends(  # noqa: B008
        get_harvest_bucket_writer
    ),
) -> ExtractionService:
    """FastAPI dependency providing ExtractionService (ADR-070, ADR-071, ADR-074).

    Persistence is inlined inside the service: `upsert_and_embed` writes
    place + embedding, `save_places` links to user_places with
    `approved=False` (catching DuplicateUserPlaceError to filter
    conflicts), `event_dispatcher` fires PlaceSaved per newly linked
    place. v2 services are the single seam — extraction never reaches
    the UserPlacesRepo directly. `result_cache` (ADR-074) lets the
    second-and-later users who share the same URL skip the pipeline.
    `harvest_writer` snapshots the share's content to object storage and
    the service dispatches a background harvest event (ADR-121).
    """
    return ExtractionService(
        pipeline=pipeline,
        upsert_service=upsert_service,
        user_places_service=user_places_service,
        event_dispatcher=event_dispatcher,
        result_cache=result_cache,
        harvest_writer=harvest_writer,
    )


# ---------------------------------------------------------------------------
# Agent graph + ChatService — consume get_extraction_service so they live
# below it.
# ---------------------------------------------------------------------------


def get_home_service(
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
) -> HomeService:
    """FastAPI dependency providing HomeService (ADR-111).

    Wraps the process-wide `get_instructor_client("home_suggester")` client,
    the shared Redis client, and the geocoding boundary (for the
    coordinates→city fallback — reverse lookups sit behind the Redis bucket
    cache, so repeat opens from the same spot cost nothing). `taste_service`
    is pulled from its existing factory so the request graph reuses one
    instance (ADR-019). The service fails open, so a missing Redis URL /
    unreachable geocoder degrades to the static fallback rather than
    erroring.
    """
    return HomeService(
        instructor_client=get_instructor_client("home_suggester"),
        taste_service=taste_service,
        geocoder=get_geocoder(),
        redis=get_redis_client(get_env().REDIS_URL),
        config=get_config().home,
    )


def get_candidate_namer_service() -> CandidateNamerService:
    """FastAPI dependency providing CandidateNamerService.

    Wraps the process-wide `get_instructor_client("candidate_namer")`
    Instructor client. Safe to construct per request — the underlying
    OpenAI/Instructor client is cached at the provider layer.
    """
    return CandidateNamerService(
        instructor_client=get_instructor_client("candidate_namer"),
    )


def get_agent_graph(
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    checkpointer: Any = Depends(get_agent_checkpointer),  # noqa: B008
    hybrid_search: HybridSearchService = Depends(get_hybrid_search_service),  # noqa: B008
    places_search_factory: SearchServiceFactory = Depends(  # noqa: B008
        get_search_service_factory
    ),
    candidate_namer: CandidateNamerService = Depends(  # noqa: B008
        get_candidate_namer_service
    ),
    research_service: ResearchService = Depends(get_research_service),  # noqa: B008
    area_suggestion_service: AreaSuggestionService = Depends(  # noqa: B008
        get_area_suggestion_service
    ),
) -> Any:
    """Build the agent StateGraph per-request.

    Compiling per-request reuses the process-scoped checkpointer that
    owns its own psycopg pool, and binds request-scoped tool services
    (`HybridSearchService` for `find_saved`; `places_search_factory`
    for `suggest_places` — the fan-out runs N parallel provider
    lookups so each one must open its own session, mirroring the
    extraction pipeline pattern, ADR-072). `CandidateNamerService` is
    process-safe but is still resolved through `Depends()` so the
    wiring stays in one place.

    Two plan-tier entitlements shape the graph per request:
    `discovery_enabled` decides whether the external-provider tools are
    bound; `advanced_models_enabled` selects the higher-quality
    orchestrator model via the config-driven `orchestrator_advanced`
    role (no model names hardcoded). `get_langchain_chat_model` is
    cached per role string, so both model tiers stay warm.
    """
    if checkpointer is None:
        return None
    from kebi.core.agent.graph import build_graph
    from kebi.core.agent.tools import build_tools
    from kebi.providers.llm import get_langchain_chat_model

    # Advanced tier selects the `orchestrator_advanced` role (emitted at boot
    # from the orchestrator block's `advanced` option). Fall back to the
    # standard orchestrator if a deploy did not configure one, so a missing
    # `advanced` key degrades gracefully rather than 500-ing the turn.
    orchestrator_role = "orchestrator"
    if identity.advanced_models_enabled and "orchestrator_advanced" in (
        get_config().models
    ):
        orchestrator_role = "orchestrator_advanced"
    llm = get_langchain_chat_model(orchestrator_role)
    resolver_llm = get_langchain_chat_model("location_resolver")
    return build_graph(
        llm,
        build_tools(
            hybrid_search,
            candidate_namer,
            places_search_factory,
            research_service,
            area_suggestion_service,
            discovery_enabled=identity.discovery_enabled,
        ),
        checkpointer,
        resolver_llm,
        get_geocoder(),
        get_area_service(),
    )


def get_consult_quota_service() -> ConsultQuotaService:
    """FastAPI dependency providing the Redis-backed consult quota service.

    Shares the process-wide async Redis client (connection pool reused
    across requests, ADR-019).
    """
    return ConsultQuotaService(redis=get_redis_client(get_env().REDIS_URL))


async def get_chat_service(
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
    memory_service: UserMemoryService = Depends(get_user_memory_service),  # noqa: B008
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
    config: AppConfig = Depends(get_config),  # noqa: B008
    agent_graph: Any = Depends(get_agent_graph),  # noqa: B008
) -> ChatService:
    """FastAPI dependency for ChatService (ADR-052/073/075/078)."""
    return ChatService(
        event_dispatcher=event_dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        config=config,
        agent_graph=agent_graph,
    )
