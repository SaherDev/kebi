"""Debounce mechanism for taste profile regeneration (ADR-058).

Process-local dict[user_id, asyncio.Task] with cancellation.
Module-level singleton; wire cancel_all() into FastAPI lifespan shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class RegenDebouncer:
    """Debounce taste profile regeneration per user_id."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Task[None]] = {}

    def schedule(
        self,
        user_id: str,
        coro_factory: Callable[[], Awaitable[None]],
        delay_seconds: float,
    ) -> None:
        """Cancel existing task for user_id if pending, schedule new delayed task."""
        existing = self._pending.get(user_id)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _delayed() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                await coro_factory()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Regen failed for user %s", user_id)
            finally:
                self._pending.pop(user_id, None)

        self._pending[user_id] = asyncio.create_task(_delayed())

    async def cancel_all(self) -> None:
        """Cancel all in-flight tasks. Called from FastAPI lifespan shutdown."""
        for task in self._pending.values():
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending.values(), return_exceptions=True)
        self._pending.clear()

    def cancel_pending(self, user_id: str) -> None:
        """Cancel and forget the pending regen task for a single user.

        Used by the delete-user sweep so the cancelled user doesn't get a
        regen fired against now-deleted rows. No-op if no task is pending.
        """
        task = self._pending.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()


# Module-level singleton
regen_debouncer = RegenDebouncer()
