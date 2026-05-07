import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from fastapi import APIRouter, FastAPI
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

from kebi.api.errors import register_error_handlers
from kebi.api.routes.chat import router as chat_router
from kebi.api.routes.extraction import router as extraction_router
from kebi.api.routes.signal import router as signal_router
from kebi.api.routes.user import router as user_router

# Agent checkpointer warmup (feature 028 M6). The compiled StateGraph is
# built per-request in `get_agent_graph` so its tools see request-scoped
# DB sessions; only the checkpointer (which owns its own psycopg pool) is
# process-scoped.
from kebi.core.agent.checkpointer import build_checkpointer
from kebi.core.config import get_config
from kebi.db.session import _get_session_factory

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

    yield

    # ADR-058: cancel in-flight taste regen debounce tasks on shutdown
    from kebi.core.taste.debounce import regen_debouncer

    await regen_debouncer.cancel_all()

    # Close the checkpointer connection pool gracefully.
    checkpointer = getattr(app.state, "agent_checkpointer", None)
    if checkpointer is not None:
        try:
            pool = getattr(checkpointer, "conn", None)
            if pool is not None:
                await pool.close()
        except Exception:
            logger.exception("Error closing checkpointer pool on shutdown")


app = FastAPI(
    title=_app_meta.name,
    version=_version,
    description=_app_meta.description,
    lifespan=lifespan,
)

router = APIRouter(prefix=_app_meta.api_prefix)


@router.get("/health")
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


# Include routers (ADR-052: /v1/chat handles conversational traffic)
router.include_router(chat_router, prefix="")
router.include_router(extraction_router, prefix="")
router.include_router(signal_router, prefix="")
router.include_router(user_router, prefix="")
app.include_router(router)

# Register error handlers
register_error_handlers(app)
