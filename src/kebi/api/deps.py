"""FastAPI dependencies for route handlers (ADR-019)."""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from kebi.core.chat.service import ChatService
from kebi.core.config import AppConfig, ExtractionConfig, get_config, get_env
from kebi.core.events.dispatcher import EventDispatcher
from kebi.core.events.handlers import EventHandlers
from kebi.core.extraction.enrichment_level import EnrichmentLevel
from kebi.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    deep_summary,
    inline_summary,
)
from kebi.core.extraction.result_cache import ExtractionResultCache
from kebi.core.extraction.service import ExtractionService
from kebi.core.extraction.status_repository import ExtractionStatusRepository
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
from kebi.core.user.service import UserDataDeletionService
from kebi.db.session import _get_session_factory, get_session
from kebi.providers import get_instructor_client
from kebi.providers.cache import CacheBackend
from kebi.providers.embeddings import EmbedderProtocol, get_embedder
from kebi.providers.http_client import get_shared_http_client
from kebi.providers.llm import get_transcription_client, get_vision_extractor
from kebi.providers.redis_cache import RedisCacheBackend, get_redis_client


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


def get_status_repo(
    cache: CacheBackend = Depends(get_cache_backend),  # noqa: B008
) -> ExtractionStatusRepository:
    """FastAPI dependency providing ExtractionStatusRepository."""
    return ExtractionStatusRepository(cache=cache)


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
) -> EventDispatcher:
    """FastAPI dependency providing a fully wired EventDispatcher (ADR-043).

    Pulls `taste_service` and `memory_service` from the existing
    `Depends(...)` factories so FastAPI's per-request dedup hands out the
    same instances the rest of the request graph already uses (ADR-019).
    Both services use session_factory internally — each repo method opens
    its own session, so background tasks don't depend on request session.
    `BackgroundTasks` stays request-scoped (FastAPI requirement).
    """
    handlers = EventHandlers(
        taste_service=taste_service,
        memory_service=memory_service,
    )

    dispatcher = EventDispatcher(background_tasks=background_tasks)
    for event_type in (
        "place_saved",
        "recommendation_accepted",
        "recommendation_rejected",
    ):
        dispatcher.register_handler(event_type, handlers.on_taste_signal)
    dispatcher.register_handler(
        "turn_completed",
        handlers.on_turn_completed,  # type: ignore[arg-type]
    )

    return dispatcher


def _make_inline_level() -> EnrichmentLevel:
    """Build the inline enrichment level with singleton circuit breakers.

    Enrichers are pure caption/text producers. NER lives at the
    pipeline as the shared finalizer — runs after every executed level.
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
    """Build the URL-only deep enrichment level (subtitle/audio/vision).

    Subtitle and Whisper are pure text producers — they populate
    `context.transcript`. Vision goes image → place names directly via
    a vision LLM (no text intermediate). NER lives at the pipeline as
    the shared finalizer — runs after this level, sees the
    just-populated transcript alongside any caption / supplementary
    text, and emits one consolidated NER call.
    """
    from kebi.core.extraction.enrichers.subtitle_check import SubtitleCheckEnricher
    from kebi.core.extraction.enrichers.vision_frames import VisionFramesEnricher
    from kebi.core.extraction.enrichers.vision_images import VisionImagesEnricher
    from kebi.core.extraction.enrichers.whisper_audio import WhisperAudioEnricher

    vision_extractor = get_vision_extractor()
    return EnrichmentLevel(
        name="deep_enrichment",
        enrichers=[
            SubtitleCheckEnricher(),
            WhisperAudioEnricher(
                transcription_client=get_transcription_client(),
            ),
            VisionFramesEnricher(vision_extractor=vision_extractor),
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
    """
    return SignalService(event_dispatcher=event_dispatcher)


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

    Sweeps the four user-scoped tables in one transaction (interactions,
    user_memories, taste_model, user_places), then deletes the LangGraph
    checkpoint thread, then cancels any pending taste-regen task. The
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


def get_user_places_service(
    places_repo: PlacesRepo = Depends(get_places_repo),  # noqa: B008
    user_places_repo: UserPlacesRepo = Depends(get_user_places_repo),  # noqa: B008
) -> UserPlacesService:
    """FastAPI dependency providing UserPlacesService (places)."""
    return UserPlacesService(
        places_repo=places_repo,
        user_places_repo=user_places_repo,
    )


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
) -> ExtractionPipeline:
    """FastAPI dependency providing ExtractionPipeline with all levels wired.

    Per ADR-070, the search step delegates to
    `places.PlacesSearchService` (DB-first lookup with cache
    overlay, provider fallback, upsert). Extraction never calls Google
    directly anymore.
    """
    from kebi.core.extraction.enrichers.llm_picker import LLMPlacePicker

    return ExtractionPipeline(
        levels=[_get_inline_level(), _get_deep_level()],
        search_service=search_service,
        picker=LLMPlacePicker(
            instructor_client=get_instructor_client("extractor"),
            confidence_config=extraction_config.confidence,
        ),
        extraction_config=extraction_config,
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
    status_repo: ExtractionStatusRepository = Depends(get_status_repo),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
    result_cache: ExtractionResultCache = Depends(  # noqa: B008
        get_extraction_result_cache
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
    """
    return ExtractionService(
        pipeline=pipeline,
        upsert_service=upsert_service,
        user_places_service=user_places_service,
        status_repo=status_repo,
        event_dispatcher=event_dispatcher,
        result_cache=result_cache,
    )


# ---------------------------------------------------------------------------
# Agent graph + ChatService — consume get_extraction_service so they live
# below it.
# ---------------------------------------------------------------------------


def get_agent_graph(
    checkpointer: Any = Depends(get_agent_checkpointer),  # noqa: B008
) -> Any:
    """Build the agent StateGraph per-request.

    Compiling per-request reuses the process-scoped checkpointer that
    owns its own psycopg pool. The agent has no tools since ADR-075
    (recall + consult removed; save was removed earlier by ADR-073), so
    `build_tools()` returns `[]` — the graph is a zero-tool
    conversational Q&A surface.
    """
    if checkpointer is None:
        return None
    from kebi.core.agent.graph import build_graph
    from kebi.core.agent.tools import build_tools
    from kebi.providers.llm import get_langchain_chat_model

    llm = get_langchain_chat_model("orchestrator")
    return build_graph(llm, build_tools(), checkpointer)


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
