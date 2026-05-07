"""Real-DB integration test for UserDataDeletionService — proves
`adelete_thread` actually wipes the three per-thread checkpoint tables
and leaves other users' rows intact.

Requires docker-compose Postgres (or DATABASE_URL) reachable. Skipped if
the connection fails — matches the local-dev workflow where
`docker compose up -d postgres` is a prerequisite. Mirrors the skip
pattern in tests/core/agent/test_checkpointer.py.
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint

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


async def _seed_thread(saver: object, thread_id: str) -> None:
    """Write one minimal checkpoint for the given thread_id via the saver."""
    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
        }
    }
    metadata = {"source": "input", "step": 0, "writes": {}, "parents": {}}
    await saver.aput(config, empty_checkpoint(), metadata, {})  # type: ignore[attr-defined]


async def _count_rows(table: str, thread_id: str) -> int:
    import psycopg

    url = _normalize_postgres_url(get_env().DATABASE_URL)
    async with (
        await psycopg.AsyncConnection.connect(url) as conn,
        conn.cursor() as cur,
    ):
        await cur.execute(
            f"SELECT count(*) FROM {table} WHERE thread_id = %s",  # noqa: S608
            (thread_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


@needs_postgres
async def test_adelete_thread_wipes_only_target_user_across_all_three_tables() -> None:
    """End-to-end check: write checkpoints for two threads, delete one,
    assert all three per-thread tables drop the deleted user's rows
    while the other user's rows survive."""
    saver = await build_checkpointer()
    assert saver is not None

    # Use unique IDs per run so we don't collide with leftover dev data.
    user_a = f"int-test-delete-{uuid.uuid4()}"
    user_b = f"int-test-keep-{uuid.uuid4()}"

    await _seed_thread(saver, user_a)
    await _seed_thread(saver, user_b)

    # Sanity: both users have at least one checkpoint row before delete.
    assert await _count_rows("checkpoints", user_a) > 0
    assert await _count_rows("checkpoints", user_b) > 0

    await saver.adelete_thread(user_a)  # type: ignore[attr-defined]

    # Target user wiped from all three per-thread tables.
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert await _count_rows(table, user_a) == 0, (
            f"{table} still has rows for {user_a} after adelete_thread"
        )

    # Other user untouched in checkpoints (the table aput definitely populates).
    assert await _count_rows("checkpoints", user_b) > 0, (
        f"checkpoints lost rows for {user_b} — adelete_thread over-deleted"
    )

    # Cleanup so the row doesn't pile up in the dev DB.
    await saver.adelete_thread(user_b)  # type: ignore[attr-defined]
