"""FastAPI dependencies for route handlers (ADR-019)."""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from totoro_ai.core.chat.service import ChatService
from totoro_ai.core.config import AppConfig, ExtractionConfig, get_config, get_env
from totoro_ai.core.consult.service import ConsultService
from totoro_ai.core.events.dispatcher import EventDispatcher
from totoro_ai.core.events.handlers import EventHandlers
from totoro_ai.core.extraction.enrichment_level import EnrichmentLevel
from totoro_ai.core.extraction.extraction_pipeline import (
    ExtractionPipeline,
    deep_summary,
    inline_summary,
)
from totoro_ai.core.extraction.persistence import ExtractionPersistenceService
from totoro_ai.core.extraction.service import ExtractionService
from totoro_ai.core.extraction.status_repository import ExtractionStatusRepository
from totoro_ai.core.memory.buffer import MessageBuffer
from totoro_ai.core.memory.extractor import MemoryExtractor
from totoro_ai.core.memory.repository import SQLAlchemyUserMemoryRepository
from totoro_ai.core.memory.service import UserMemoryService
from totoro_ai.core.places import GooglePlacesClient, PlacesService
from totoro_ai.core.places.cache import PlacesCache
from totoro_ai.core.places.repository import PlacesRepository
from totoro_ai.core.places_v2 import (
    CachedEmbedder,
    EmbeddingsRepo,
    HybridSearchRepo,
    HybridSearchService,
    PlacesRepo,
    PlacesSearchService,
    PlaceUpsertService,
    RedisPlacesCache,
    UserPlacesRepo,
    UserPlacesService,
)
from totoro_ai.core.places_v2 import (
    EmbeddingService as EmbeddingServiceV2,
)
from totoro_ai.core.places_v2 import (
    GooglePlacesClient as GooglePlacesClientV2,
)
from totoro_ai.core.recall.service import RecallService
from totoro_ai.core.signal.service import SignalService
from totoro_ai.core.taste.debounce import regen_debouncer
from totoro_ai.core.taste.service import TasteModelService
from totoro_ai.core.user.service import UserDataDeletionService
from totoro_ai.db.repositories import (
    EmbeddingRepository,
    SQLAlchemyEmbeddingRepository,
    SQLAlchemyRecallRepository,
)
from totoro_ai.db.repositories.recommendation_repository import (
    RecommendationRepository,
    SQLAlchemyRecommendationRepository,
)
from totoro_ai.db.session import _get_session_factory, get_session
from totoro_ai.providers import get_instructor_client
from totoro_ai.providers.cache import CacheBackend
from totoro_ai.providers.embeddings import EmbedderProtocol, get_embedder
from totoro_ai.providers.llm import get_transcription_client, get_vision_extractor
from totoro_ai.providers.redis_cache import RedisCacheBackend


def get_taste_service() -> TasteModelService:
    """FastAPI dependency providing TasteModelService.

    Uses session_factory so each repo method opens its own session.
    """
    return TasteModelService(session_factory=_get_session_factory())


def get_cache_backend() -> CacheBackend:
    """FastAPI dependency providing CacheBackend (RedisCacheBackend by default)."""
    return RedisCacheBackend(url=get_env().REDIS_URL)


def get_status_repo(
    cache: CacheBackend = Depends(get_cache_backend),  # noqa: B008
) -> ExtractionStatusRepository:
    """FastAPI dependency providing ExtractionStatusRepository."""
    return ExtractionStatusRepository(cache=cache)


def _build_places_cache() -> PlacesCache:
    """Construct a PlacesCache from the Redis URL in secrets.

    A fresh `redis.asyncio.Redis` client is built per call. The async client
    reuses its connection pool internally, so per-request construction is
    cheap and avoids the "client bound to the wrong event loop" pitfall.
    """
    from redis.asyncio import Redis

    redis_client = Redis.from_url(get_env().REDIS_URL, decode_responses=True)
    return PlacesCache(redis_client)


def get_places_service(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlacesService:
    """FastAPI dependency providing `PlacesService` (ADR-054, feature 019).

    Wires the `PlacesRepository`, `PlacesCache`, and `GooglePlacesClient` so
    every caller consuming `PlacesService` sees a fully functional
    `enrich_batch` in both recall (`geo_only=True`) and consult modes.
    """
    return PlacesService(
        repo=PlacesRepository(db_session),
        cache=_build_places_cache(),
        client=GooglePlacesClient(),
    )


def get_embedding_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EmbeddingRepository:
    """FastAPI dependency providing EmbeddingRepository."""
    return SQLAlchemyEmbeddingRepository(db_session)


def _build_message_buffer() -> MessageBuffer:
    """Construct a per-user message buffer backed by a fresh Redis client.

    Per `_build_places_cache`'s pattern: build a fresh `redis.asyncio.Redis`
    per call. The async client reuses its connection pool internally, so
    per-request construction is cheap and avoids the wrong-event-loop pitfall.
    """
    from redis.asyncio import Redis

    cfg = get_config()
    redis_client = Redis.from_url(get_env().REDIS_URL, decode_responses=True)
    return MessageBuffer(
        redis=redis_client,
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


def get_embedder_dep() -> EmbedderProtocol:
    """FastAPI dependency providing EmbedderProtocol."""
    return get_embedder()


async def get_event_dispatcher(
    background_tasks: BackgroundTasks,
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EventDispatcher:
    """FastAPI dependency providing a fully wired EventDispatcher (ADR-043).

    ExtractionPersistenceService is constructed inline here (not via
    Depends(get_extraction_persistence)) to avoid a circular dependency:
    get_event_dispatcher <- get_extraction_persistence <- get_event_dispatcher.

    Taste and memory services use session_factory — each repo method opens
    its own session, so background tasks don't depend on request session.
    """
    cfg = get_config()
    sf = _get_session_factory()
    taste_service = TasteModelService(session_factory=sf)
    memory_service = UserMemoryService(
        repo=SQLAlchemyUserMemoryRepository(sf),
        extractor=MemoryExtractor(get_instructor_client("memory_extractor")),
        confidence_config=cfg.memory.confidence,
        buffer=_build_message_buffer(),
        debounce_messages=cfg.memory.extraction.debounce_messages,
    )
    handlers = EventHandlers(
        taste_service=taste_service,
        memory_service=memory_service,
    )

    dispatcher = EventDispatcher(background_tasks=background_tasks)
    for event_type in (
        "place_saved",
        "recommendation_accepted",
        "recommendation_rejected",
        "onboarding_signal",
    ):
        dispatcher.register_handler(event_type, handlers.on_taste_signal)
    dispatcher.register_handler(
        "turn_completed",
        handlers.on_turn_completed,  # type: ignore[arg-type]
    )
    dispatcher.register_handler(
        "chip_confirmed",
        handlers.on_chip_confirmed,
    )

    return dispatcher


def get_places_cache_dep() -> PlacesCache:
    """FastAPI dependency providing `PlacesCache`.

    Extraction persistence takes this separately from `PlacesService` so
    it can write Tier 2 geo data directly after Google validation (ADR-057
    follow-up) — the service facade is the query path, the cache is the
    write path.
    """
    return _build_places_cache()


def get_extraction_persistence(
    places_service: PlacesService = Depends(get_places_service),  # noqa: B008
    places_cache: PlacesCache = Depends(get_places_cache_dep),  # noqa: B008
    embedding_repo: EmbeddingRepository = Depends(get_embedding_repo),  # noqa: B008
    embedder: EmbedderProtocol = Depends(get_embedder_dep),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
) -> ExtractionPersistenceService:
    """FastAPI dependency providing ExtractionPersistenceService."""
    return ExtractionPersistenceService(
        places_service=places_service,
        places_cache=places_cache,
        embedding_repo=embedding_repo,
        embedder=embedder,
        event_dispatcher=event_dispatcher,
    )


def _make_inline_level() -> EnrichmentLevel:
    """Build the inline enrichment level with singleton circuit breakers.

    Enrichers are pure caption/text producers. NER lives at the
    pipeline as the shared finalizer — runs after every executed level.
    """
    from totoro_ai.core.extraction.circuit_breaker import (
        CircuitBreakerEnricher,
        ParallelEnricherGroup,
    )
    from totoro_ai.core.extraction.enrichers.google_maps_list import (
        GoogleMapsListEnricher,
    )
    from totoro_ai.core.extraction.enrichers.tiktok_oembed import TikTokOEmbedEnricher
    from totoro_ai.core.extraction.enrichers.ytdlp_metadata import YtDlpMetadataEnricher

    return EnrichmentLevel(
        name="enrich",
        enrichers=[
            ParallelEnricherGroup(
                [
                    CircuitBreakerEnricher(TikTokOEmbedEnricher()),
                    CircuitBreakerEnricher(YtDlpMetadataEnricher()),
                    CircuitBreakerEnricher(GoogleMapsListEnricher()),
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
    from totoro_ai.core.extraction.enrichers.subtitle_check import SubtitleCheckEnricher
    from totoro_ai.core.extraction.enrichers.vision_frames import VisionFramesEnricher
    from totoro_ai.core.extraction.enrichers.whisper_audio import WhisperAudioEnricher

    return EnrichmentLevel(
        name="deep_enrichment",
        enrichers=[
            SubtitleCheckEnricher(),
            WhisperAudioEnricher(
                transcription_client=get_transcription_client(),
            ),
            VisionFramesEnricher(vision_extractor=get_vision_extractor()),
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


def get_extraction_pipeline(
    extraction_config: ExtractionConfig = Depends(get_extraction_config),  # noqa: B008
) -> ExtractionPipeline:
    """FastAPI dependency providing ExtractionPipeline with all levels wired."""
    from totoro_ai.core.extraction.enrichers.llm_ner import LLMNEREnricher
    from totoro_ai.core.extraction.validator import GooglePlacesValidator
    from totoro_ai.core.places import GooglePlacesClient

    validator = GooglePlacesValidator(
        places_client=GooglePlacesClient(),
        confidence_config=extraction_config.confidence,
    )
    return ExtractionPipeline(
        levels=[_get_inline_level(), _get_deep_level()],
        validator=validator,
        extraction_config=extraction_config,
        finalizer=LLMNEREnricher(
            instructor_client=get_instructor_client("extractor")
        ),
    )


def get_extraction_service(
    pipeline: ExtractionPipeline = Depends(get_extraction_pipeline),  # noqa: B008
    persistence: ExtractionPersistenceService = Depends(  # noqa: B008
        get_extraction_persistence
    ),
    status_repo: ExtractionStatusRepository = Depends(get_status_repo),  # noqa: B008
) -> ExtractionService:
    """FastAPI dependency providing ExtractionService."""
    return ExtractionService(
        pipeline=pipeline, persistence=persistence, status_repo=status_repo
    )


async def get_recall_service(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
    config: AppConfig = Depends(get_config),  # noqa: B008
    places_service: PlacesService = Depends(get_places_service),  # noqa: B008
) -> RecallService:
    """FastAPI dependency providing a fully wired RecallService."""
    return RecallService(
        embedder=get_embedder(),
        recall_repo=SQLAlchemyRecallRepository(db_session),
        config=config.recall,
        places_service=places_service,
    )


def get_recommendation_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RecommendationRepository:
    """FastAPI dependency providing a fully wired RecommendationRepository (ADR-060)."""
    return SQLAlchemyRecommendationRepository(db_session)


def get_signal_service(
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
) -> SignalService:
    """FastAPI dependency providing SignalService (ADR-060 + feature 023).

    SignalService owns the RecommendationRepository internally via
    session_factory. It also delegates chip_confirm handling to the
    TasteModelService (for chip read and merge persistence).
    """
    return SignalService(
        session_factory=_get_session_factory(),
        event_dispatcher=event_dispatcher,
        taste_service=taste_service,
    )


async def get_consult_service(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
    recommendation_repo: RecommendationRepository = Depends(get_recommendation_repo),  # noqa: B008
) -> ConsultService:
    """FastAPI dependency providing a fully wired ConsultService (feature 028 M4).

    Wires the 4-phase pipeline dependencies: places client, places service,
    taste model service (active-tier chip filter only, ADR-061), and
    recommendation repository (ADR-060). IntentParser, RecallService, and
    UserMemoryService are no longer held by the consult service — the
    caller supplies pre-parsed `query`, pre-loaded `saved_places`, and an
    optional `preference_context`.
    """
    places_service = PlacesService(
        repo=PlacesRepository(db_session),
        cache=_build_places_cache(),
        client=GooglePlacesClient(),
    )
    return ConsultService(
        places_client=GooglePlacesClient(),
        places_service=places_service,
        taste_service=TasteModelService(session_factory=_get_session_factory()),
        recommendation_repo=recommendation_repo,
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

    Sweeps the five AI tables in one transaction (embeddings cascade
    from places via FK), then deletes the LangGraph checkpoint thread,
    then cancels any pending taste-regen task. Erases AI-owned data
    only — NestJS is responsible for deleting the user account itself.
    Hard-delete only, sync sweep, 204 No Content.
    """
    return UserDataDeletionService(
        session_factory=_get_session_factory(),
        checkpointer=checkpointer,
        regen_debouncer=regen_debouncer,
    )


def get_agent_graph(
    recall_service: RecallService = Depends(get_recall_service),  # noqa: B008
    extraction_service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
    consult_service: ConsultService = Depends(get_consult_service),  # noqa: B008
    checkpointer: Any = Depends(get_agent_checkpointer),  # noqa: B008
) -> Any:
    """Build the agent StateGraph per-request.

    Compiling per-request keeps tool-bound services request-scoped (fresh
    SQLAlchemy sessions, real EventDispatcher) while reusing the
    process-scoped checkpointer that owns its own psycopg pool.
    Compilation is cheap compared to the LLM call it shepherds, so the
    latency cost is negligible.
    """
    if checkpointer is None:
        return None
    from totoro_ai.core.agent.graph import build_graph
    from totoro_ai.core.agent.tools import build_tools
    from totoro_ai.providers.llm import get_langchain_chat_model

    tools = build_tools(recall_service, extraction_service, consult_service)
    llm = get_langchain_chat_model("orchestrator")
    return build_graph(llm, tools, checkpointer)


async def get_chat_service(
    extraction_service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
    consult_service: ConsultService = Depends(get_consult_service),  # noqa: B008
    recall_service: RecallService = Depends(get_recall_service),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
    memory_service: UserMemoryService = Depends(get_user_memory_service),  # noqa: B008
    taste_service: TasteModelService = Depends(get_taste_service),  # noqa: B008
    places_service: PlacesService = Depends(get_places_service),  # noqa: B008
    config: AppConfig = Depends(get_config),  # noqa: B008
    agent_graph: Any = Depends(get_agent_graph),  # noqa: B008
) -> ChatService:
    """FastAPI dependency providing a fully wired ChatService (ADR-019, ADR-052).

    Feature 028 M11 (ADR-065): legacy intent_parser and assistant_service
    removed — all traffic routed to the agent pipeline.
    """
    return ChatService(
        extraction_service=extraction_service,
        consult_service=consult_service,
        recall_service=recall_service,
        event_dispatcher=event_dispatcher,
        memory_service=memory_service,
        taste_service=taste_service,
        places_service=places_service,
        config=config,
        agent_graph=agent_graph,
    )


# ---------------------------------------------------------------------------
# places_v2 dependencies
# ---------------------------------------------------------------------------


def get_places_v2_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlacesRepo:
    """FastAPI dependency providing PlacesRepo (places_v2 table)."""
    return PlacesRepo(db_session)


def get_user_places_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> UserPlacesRepo:
    """FastAPI dependency providing UserPlacesRepo (user_places table)."""
    return UserPlacesRepo(db_session)


def get_places_v2_cache() -> RedisPlacesCache:
    """FastAPI dependency providing RedisPlacesCache (place_v2: key prefix).

    Backed by a process-wide Redis client owned by places_v2 itself.
    """
    return RedisPlacesCache.from_url(get_env().REDIS_URL)


def get_google_places_client_v2() -> GooglePlacesClientV2:
    """FastAPI dependency providing GooglePlacesClient (places_v2).

    Backed by a process-wide httpx.AsyncClient owned by places_v2 itself.
    """
    return GooglePlacesClientV2(api_key=get_env().GOOGLE_API_KEY or "")


def get_place_upsert_service(
    repo: PlacesRepo = Depends(get_places_v2_repo),  # noqa: B008
) -> PlaceUpsertService:
    """FastAPI dependency providing PlaceUpsertService (places_v2)."""
    return PlaceUpsertService(repo=repo)


def get_places_search_service(
    repo: PlacesRepo = Depends(get_places_v2_repo),  # noqa: B008
    cache: RedisPlacesCache = Depends(get_places_v2_cache),  # noqa: B008
    client: GooglePlacesClientV2 = Depends(get_google_places_client_v2),  # noqa: B008
    upsert_service: PlaceUpsertService = Depends(  # noqa: B008
        get_place_upsert_service
    ),
) -> PlacesSearchService:
    """FastAPI dependency providing PlacesSearchService (places_v2)."""
    return PlacesSearchService(
        repo=repo,
        cache=cache,
        client=client,
        upsert_service=upsert_service,
    )


def get_user_places_service(
    places_repo: PlacesRepo = Depends(get_places_v2_repo),  # noqa: B008
    user_places_repo: UserPlacesRepo = Depends(get_user_places_repo),  # noqa: B008
) -> UserPlacesService:
    """FastAPI dependency providing UserPlacesService (places_v2)."""
    return UserPlacesService(
        places_repo=places_repo,
        user_places_repo=user_places_repo,
    )


# ---------------------------------------------------------------------------
# places_v2 — embeddings + hybrid search
# ---------------------------------------------------------------------------


def get_embeddings_repo_v2(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EmbeddingsRepo:
    """FastAPI dependency providing EmbeddingsRepo (place_embeddings_v2)."""
    return EmbeddingsRepo(db_session)


def get_embedding_service_v2(
    repo: EmbeddingsRepo = Depends(get_embeddings_repo_v2),  # noqa: B008
    embedder: EmbedderProtocol = Depends(get_embedder_dep),  # noqa: B008
    config: AppConfig = Depends(get_config),  # noqa: B008
) -> EmbeddingServiceV2:
    """FastAPI dependency providing EmbeddingService (places_v2 documents).

    Doc embeds run at save time and rarely repeat — no cache wrapper here;
    the diff-then-embed text_hash check on the repo already short-circuits
    no-op re-saves.
    """
    return EmbeddingServiceV2(
        repo=repo,
        embedder=embedder,
        model_name=config.models["embedder"].model,
    )


def get_query_embedder_v2() -> EmbedderProtocol:
    """Cached query embedder backed by Redis (90-day TTL).

    Wraps the configured embedder so HybridSearchService skips Voyage
    on repeated queries. ``model_name`` is part of the cache key so
    swapping embedders later never serves a vector from the wrong space.
    """
    return CachedEmbedder.from_url(
        embedder=get_embedder(),
        url=get_env().REDIS_URL,
        model_name=get_config().models["embedder"].model,
    )


def get_hybrid_search_repo(
    db_session: AsyncSession = Depends(get_session),  # noqa: B008
) -> HybridSearchRepo:
    """FastAPI dependency providing HybridSearchRepo (places_v2)."""
    return HybridSearchRepo(db_session)


def get_hybrid_search_service(
    repo: HybridSearchRepo = Depends(get_hybrid_search_repo),  # noqa: B008
    embedder: EmbedderProtocol = Depends(get_query_embedder_v2),  # noqa: B008
) -> HybridSearchService:
    """FastAPI dependency providing HybridSearchService (places_v2).

    Uses the cached query embedder so repeated agent queries skip
    Voyage on the hot path.
    """
    return HybridSearchService(repo=repo, embedder=embedder)
