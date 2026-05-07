"""Integration tests for AsyncPostgresSaver idempotency (feature 027 M3, SC-010).

Requires docker-compose Postgres (or DATABASE_URL) reachable. Skipped if
the connection fails — matches local-dev workflow where `docker compose
up -d postgres` is a prerequisite.
"""

from __future__ import annotations

import pytest

from kebi.core.agent.checkpointer import (
    _normalize_postgres_url,
    build_checkpointer,
)
from kebi.core.config import get_env


def _postgres_reachable() -> bool:
    try:
        import asyncio

        import psycopg

        url = _normalize_postgres_url(get_env().DATABASE_URL)

        async def _probe() -> None:
            async with (
                await psycopg.AsyncConnection.connect(url) as conn,
                conn.cursor() as cur,
            ):
                await cur.execute("SELECT 1")
                await cur.fetchone()

        asyncio.get_event_loop().run_until_complete(_probe())
        return True
    except Exception:
        return False


needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="docker-compose Postgres not reachable — run docker compose up -d postgres",
)


@needs_postgres
async def test_setup_creates_library_tables() -> None:
    """First call creates the three library-owned tables."""
    import psycopg

    saver = await build_checkpointer()
    assert saver is not None

    url = _normalize_postgres_url(get_env().DATABASE_URL)
    async with (
        await psycopg.AsyncConnection.connect(url) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename IN (
                'checkpoints', 'checkpoint_blobs', 'checkpoint_writes'
              )
            ORDER BY tablename
            """
        )
        rows = await cur.fetchall()
    names = {r[0] for r in rows}
    assert "checkpoints" in names
    assert "checkpoint_blobs" in names
    assert "checkpoint_writes" in names


@needs_postgres
async def test_setup_is_idempotent() -> None:
    """Repeated setup() calls do not raise and do not overwrite."""
    saver1 = await build_checkpointer()
    saver2 = await build_checkpointer()
    assert saver1 is not None
    assert saver2 is not None
    # Call setup() again directly — must be a no-op.
    await saver2.setup()
    await saver2.setup()


def test_normalize_postgres_url_strips_asyncpg_driver() -> None:
    assert (
        _normalize_postgres_url("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    assert (
        _normalize_postgres_url("postgresql://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
