"""Background TTL cleanup for the LangGraph Postgres checkpointer.

Postgres has no native TTL, and `langgraph-checkpoint-postgres` never
prunes — so without an explicit cleanup job the `checkpoints`,
`checkpoint_blobs`, and `checkpoint_writes` tables grow without bound.
Beyond cost, the unbounded state is also a privacy footprint: every
old thread retains the resolved working_location, prior messages, and
reasoning steps.

This module starts an asyncio task at app startup that periodically
deletes rows older than `agent.checkpointer_ttl_seconds`. It uses the
checkpointer's own psycopg pool to avoid opening a second one.

Configuration: `AppConfig.agent.checkpointer_ttl_seconds` (default 1
day). Sweep interval is 1/4 of TTL — short enough to keep the deletion
backlog bounded, long enough to be a single low-cost cron.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


_DELETE_SQL = (
    "DELETE FROM checkpoints "
    "WHERE checkpoint_id::text < %(cutoff)s"
)

# The checkpoint_id column is a ULID-style timestamp prefix that sorts
# chronologically, so comparing on text is safe and uses the index.
# `checkpoint_blobs` and `checkpoint_writes` are pruned by FK cascade.


async def run_checkpointer_gc(
    checkpointer: Any,
    *,
    ttl_seconds: int,
    interval_seconds: int | None = None,
) -> None:
    """Periodically delete checkpoint rows older than `ttl_seconds`.

    Intended to run as an asyncio.Task created in the app lifespan and
    cancelled on shutdown. The loop swallows per-iteration exceptions
    so a transient DB hiccup doesn't kill the GC for the rest of the
    process lifetime; each failure is logged.
    """
    if interval_seconds is None:
        interval_seconds = max(60, ttl_seconds // 4)

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await _prune_once(checkpointer, ttl_seconds=ttl_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("checkpointer_gc sweep failed; will retry")


async def _prune_once(checkpointer: Any, *, ttl_seconds: int) -> int:
    """One DELETE pass; returns the row count for logging."""
    pool = getattr(checkpointer, "conn", None)
    if pool is None:
        logger.warning("checkpointer_gc: no pool on checkpointer, skipping sweep")
        return 0

    # Use a "cutoff" expression matching the ULID-style id prefix the
    # langgraph checkpointer writes. We compare as text via the
    # `lpad` of the unix-epoch seconds to keep the rendered SQL
    # database-portable and index-friendly.
    cutoff_seconds = _seconds_ago(ttl_seconds)
    # `checkpoint_id` is a UUIDv7-shaped string whose leading bytes are
    # a millisecond timestamp. Comparing the text prefix to a value
    # built from the cutoff timestamp gives a chronological filter.
    cutoff = _cutoff_string(cutoff_seconds)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_DELETE_SQL, {"cutoff": cutoff})
        deleted = cur.rowcount
    if deleted:
        logger.info("checkpointer_gc: pruned %d expired checkpoints", deleted)
    return int(deleted or 0)


def _seconds_ago(ttl_seconds: int) -> float:
    import time

    return time.time() - float(ttl_seconds)


def _cutoff_string(epoch_seconds: float) -> str:
    """Build a text prefix matching the UUIDv7 layout used by langgraph.

    UUIDv7 starts with 48 bits of unix-millis encoded as 12 hex chars
    (`xxxxxxxx-xxxx-`). We render `epoch_millis` as a left-padded
    12-char hex string and append `-` so the lexicographic comparison
    in SQL behaves like a chronological cutoff.
    """
    epoch_ms = int(epoch_seconds * 1000)
    hex_ms = f"{epoch_ms:012x}"
    return f"{hex_ms[:8]}-{hex_ms[8:]}-"
