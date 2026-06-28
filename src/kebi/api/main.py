import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

# Backport of the LangGraph 0.4.x fix for a weak-reference race in the pregel
# runner: when the PregelRunner is GC'd before uvloop fires its done-callbacks,
# self.callback() returns None and the call raises TypeError. Guard it so the
# callback is silently skipped instead of crashing uvloop's Handle._run.
# Fixed upstream in LangGraph 0.4.x (walrus-operator guard); remove once we
# upgrade past 0.3.x.
try:
    from langgraph.pregel.runner import FuturesDict, _exception, _should_stop_others

    def _safe_on_done(self, task, fut):  # type: ignore[override]
        try:
            cb = self.callback()
            if cb is not None:
                cb(task, _exception(fut))
        finally:
            with self.lock:
                self.done.add(fut)
                self.counter -= 1
                if self.counter == 0 or _should_stop_others(self.done):
                    self.event.set()

    FuturesDict.on_done = _safe_on_done  # type: ignore[method-assign]
except Exception:
    pass

from kebi.api.deps import require_gateway_identity
from kebi.api.errors import register_error_handlers
from kebi.api.rate_limit import limiter
from kebi.api.routes.chat import router as chat_router
from kebi.api.routes.extraction import router as extraction_router
from kebi.api.routes.home import router as home_router
from kebi.api.routes.signal import router as signal_router
from kebi.api.routes.user import router as user_router

# Agent checkpointer warmup (feature 028 M6). The compiled StateGraph is
# built per-request in `get_agent_graph` so its tools see request-scoped
# DB sessions; only the checkpointer (which owns its own psycopg pool) is
# process-scoped.
from kebi.core.agent.checkpointer import build_checkpointer
from kebi.core.config import get_config, get_env
from kebi.db.session import _get_session_factory

# Fail-closed gateway secret check. kebi refuses to start without the
# shared secret so a misconfigured deploy can never accept anonymous
# requests. Runs at module import — before uvicorn binds the port.
if not get_env().GATEWAY_SHARED_SECRET:
    raise RuntimeError(
        "GATEWAY_SHARED_SECRET is unset. Refusing to start — kebi must "
        "never run without service-to-service auth. Generate one with "
        "`openssl rand -hex 32`, set it on both kebi and the gateway."
    )

_log_level = os.environ.get("LOG_LEVEL", "WARNING").upper()
logging.root.setLevel(getattr(logging, _log_level, logging.WARNING))
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADR-055: alignment between config.embeddings.description_fields and the
# search_vector generated column in migration a1b2c3d4e5f6. Any drift here
# means vector-similarity and FTS are searching different fields — retrieval
# quality degrades silently. The startup validator below logs CRITICAL when
# the two lists disagree.
# ---------------------------------------------------------------------------

_SEARCH_VECTOR_FIELDS = frozenset(
    {
        "place_name",
        "subcategory",
        "cuisine",
        "ambiance",
        "price_hint",
        "neighborhood",
        "city",
        "country",
    }
)


def _validate_embedding_fts_alignment() -> None:
    cfg_fields = frozenset(get_config().embeddings.description_fields)
    excluded = {"tags", "good_for", "dietary", "place_type"}
    mappable = cfg_fields - excluded
    missing = mappable - _SEARCH_VECTOR_FIELDS
    extra = _SEARCH_VECTOR_FIELDS - mappable
    if missing or extra:
        logger.critical(
            "embedding_fts_mismatch",
            extra={
                "in_config_not_in_search_vector": sorted(missing),
                "in_search_vector_not_in_config": sorted(extra),
            },
        )


_app_meta = get_config().app
try:
    _version = pkg_version("kebi")
except PackageNotFoundError:
    _version = "0.1.0"


async def _warm_agent_checkpointer(app: FastAPI) -> None:
    """Build and stash the process-scoped agent checkpointer (feature 028 M6).

    The checkpointer owns its own psycopg connection pool and is safely
    reused across requests. The compiled StateGraph itself is built
    per-request via `get_agent_graph` so its tools see request-scoped
    SQLAlchemy sessions and the real EventDispatcher.
    """
    app.state.agent_checkpointer = await build_checkpointer()
    logger.info("Agent checkpointer warmed (feature 028 M6)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    import asyncio

    from kebi.core.agent.checkpointer_gc import run_checkpointer_gc

    _validate_embedding_fts_alignment()
    # ADR-059: prompts are already loaded during get_config() at module scope
    logger.info("Loaded %d prompt templates", len(get_config().prompts))

    try:
        await _warm_agent_checkpointer(app)
    except Exception:
        logger.exception(
            "Agent checkpointer warmup failed; agent path will surface errors."
        )
        app.state.agent_checkpointer = None

    # Start the checkpointer TTL sweep so the three langgraph tables
    # don't grow unbounded. Skipped when the checkpointer isn't
    # available (test client, warmup failure) — the sweep doesn't have
    # anything to attach to in that case.
    app.state.checkpointer_gc_task = None
    if app.state.agent_checkpointer is not None:
        ttl = get_config().agent.checkpointer_ttl_seconds
        app.state.checkpointer_gc_task = asyncio.create_task(
            run_checkpointer_gc(app.state.agent_checkpointer, ttl_seconds=ttl)
        )
        logger.info("Started checkpointer_gc (ttl=%ds)", ttl)

    yield

    # ADR-058: cancel in-flight taste regen debounce tasks on shutdown
    from kebi.core.taste.debounce import regen_debouncer

    await regen_debouncer.cancel_all()

    gc_task = getattr(app.state, "checkpointer_gc_task", None)
    if gc_task is not None:
        import contextlib

        gc_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gc_task

    # Close the checkpointer connection pool gracefully.
    checkpointer = getattr(app.state, "agent_checkpointer", None)
    if checkpointer is not None:
        try:
            pool = getattr(checkpointer, "conn", None)
            if pool is not None:
                await pool.close()
        except Exception:
            logger.exception("Error closing checkpointer pool on shutdown")


# Hide the OpenAPI schema and Swagger / ReDoc UIs in production. They
# still load behind the protected router in dev so local tooling and
# Bruno collections work.
_is_prod = get_env().ENVIRONMENT == "production"
app = FastAPI(
    title=_app_meta.name,
    version=_version,
    description=_app_meta.description,
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# slowapi wiring: stash the limiter on app.state so route decorators
# pick it up, register the 429 handler globally. Per-route limits live
# alongside each handler in `api/routes/*.py`.
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    _rate_limit_exceeded_handler,  # type: ignore[arg-type]
)

# CORS. The gateway is server-to-server and does not need CORS at all;
# this exists for dev tooling (Bruno, a local frontend pointed at the
# kebi port). Production defaults to an empty allowlist — browsers will
# refuse any cross-origin request. Add origins explicitly via
# CORS_ALLOW_ORIGINS to permit a local dev frontend.
_cors_origins = [
    origin.strip()
    for origin in get_env().CORS_ALLOW_ORIGINS.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-Gateway-Token", "X-Gateway-User-Id", "Content-Type"],
)

# Two routers under the same /v1 prefix. `public_router` carries only
# unauthenticated probes (health). `protected_router` enforces
# `require_gateway_identity` on every endpoint — a missing or invalid
# X-Gateway-Token short-circuits with 401 before the route runs.
public_router = APIRouter(prefix=_app_meta.api_prefix)
protected_router = APIRouter(
    prefix=_app_meta.api_prefix,
    dependencies=[Depends(require_gateway_identity)],
)


@public_router.get("/health")
async def health() -> dict[str, str]:
    db_status = "disconnected"
    try:
        async with _get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        pass

    return {
        "status": "ok",
        "name": _app_meta.name,
        "version": _version,
        "db": db_status,
    }


# Mount the protected routes (ADR-052: /v1/chat handles conversational
# traffic). Each is mounted under the same `/v1` prefix as the public
# router — auth is enforced uniformly by the parent dependency.
protected_router.include_router(chat_router, prefix="")
protected_router.include_router(extraction_router, prefix="")
protected_router.include_router(home_router, prefix="")
protected_router.include_router(signal_router, prefix="")
protected_router.include_router(user_router, prefix="")
app.include_router(public_router)
app.include_router(protected_router)

# Register error handlers
register_error_handlers(app)
