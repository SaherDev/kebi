"""Tests for RegenDebouncer (ADR-058)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from kebi.core.taste.debounce import RegenDebouncer


class TestRegenDebouncer:
    async def test_schedule_runs_after_delay(self) -> None:
        debouncer = RegenDebouncer()
        callback = AsyncMock()
        debouncer.schedule("user1", callback, delay_seconds=0.05)
        await asyncio.sleep(0.1)
        callback.assert_awaited_once()
        await debouncer.cancel_all()

    async def test_schedule_replaces_pending_task(self) -> None:
        debouncer = RegenDebouncer()
        first = AsyncMock()
        second = AsyncMock()
        debouncer.schedule("user1", first, delay_seconds=0.5)
        debouncer.schedule("user1", second, delay_seconds=0.05)
        await asyncio.sleep(0.15)
        first.assert_not_awaited()
        second.assert_awaited_once()
        await debouncer.cancel_all()

    async def test_cancel_all_cancels_in_flight(self) -> None:
        debouncer = RegenDebouncer()
        callback = AsyncMock()
        debouncer.schedule("user1", callback, delay_seconds=1.0)
        debouncer.schedule("user2", callback, delay_seconds=1.0)
        await debouncer.cancel_all()
        callback.assert_not_awaited()
        assert len(debouncer._pending) == 0

    async def test_different_users_run_independently(self) -> None:
        debouncer = RegenDebouncer()
        cb1 = AsyncMock()
        cb2 = AsyncMock()
        debouncer.schedule("user1", cb1, delay_seconds=0.05)
        debouncer.schedule("user2", cb2, delay_seconds=0.05)
        await asyncio.sleep(0.15)
        cb1.assert_awaited_once()
        cb2.assert_awaited_once()
        await debouncer.cancel_all()

    async def test_callback_exception_logged_not_raised(self) -> None:
        debouncer = RegenDebouncer()
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        debouncer.schedule("user1", callback, delay_seconds=0.01)
        await asyncio.sleep(0.1)
        callback.assert_awaited_once()
        assert "user1" not in debouncer._pending
        await debouncer.cancel_all()
