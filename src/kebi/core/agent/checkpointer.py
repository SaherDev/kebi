"""Postgres-backed checkpointer factory (feature 027 M3, FR-030, research.md R2).

Uses `AsyncConnectionPool` so each checkpoint read/write acquires a fresh
connection from the pool rather than reusing one long-lived connection.
`from_conn_string` opens a single psycopg connection, which gets closed
after its context manager exits, causing "the connection is closed" errors
on the next LangGraph invocation. A pool avoids this by keeping N idle
connections alive and re-acquiring one per operation.

`setup()` is idempotent — repeated calls do not fail and do not overwrite
existing checkpoints. The three library-owned tables (`checkpoints`,
`checkpoint_blobs`, `checkpoint_writes`) are excluded from Alembic
autogenerate via `alembic/env.py::_include_object` (FR-031).
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from kebi.core.config import get_env


async def build_checkpointer() -> AsyncPostgresSaver:
    """Create an `AsyncPostgresSaver` backed by an `AsyncConnectionPool`.

    The pool is stored on `saver.conn` — callers that need to close it at
    shutdown access it via `saver.conn.close()`.
    """
    db_url = _normalize_postgres_url(get_env().DATABASE_URL)
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=db_url,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()
    return saver


def _normalize_postgres_url(url: str) -> str:
    """Strip the `+asyncpg` driver suffix — `AsyncPostgresSaver` uses psycopg3.

    Our SQLAlchemy config uses `postgresql+asyncpg://...`, but the
    checkpointer library expects plain `postgresql://...` or
    `postgres://...`. Returning a normalized URL lets callers reuse
    `DATABASE_URL` verbatim.
    """
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "")
    return url
