"""Unit tests for MessageBuffer (Redis-backed per-user message buffer)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kebi.core.memory.buffer import MessageBuffer


def _make_redis_mock(eval_returns: Any) -> MagicMock:
    """Mock redis.asyncio.Redis with `eval` returning the given value."""
    redis_mock = MagicMock()
    redis_mock.eval = AsyncMock(return_value=eval_returns)
    redis_mock.delete = AsyncMock(return_value=1)
    return redis_mock


async def test_append_returns_count() -> None:
    """append() returns the new buffer length from the Lua script."""
    redis_mock = _make_redis_mock(eval_returns=3)
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    count = await buffer.append("user-1", "hello")

    assert count == 3
    assert redis_mock.eval.await_count == 1
    call_args = redis_mock.eval.await_args
    # Lua script, 1 key, then key, message, ttl, max_buffer_len
    assert call_args.args[1] == 1
    assert call_args.args[2] == "memory:turns:user-1"
    assert call_args.args[3] == "hello"
    assert call_args.args[4] == "600"
    # _MAX_BUFFER_LEN is appended as the fifth arg.
    assert call_args.args[5] == "50"


async def test_append_truncates_oversized_message_client_side() -> None:
    """Per-message length cap (1000 chars) is enforced before RPUSH."""
    redis_mock = _make_redis_mock(eval_returns=1)
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    huge = "x" * 50_000
    await buffer.append("user-1", huge)

    call_args = redis_mock.eval.await_args
    pushed = call_args.args[3]
    assert len(pushed) == 1000
    assert pushed == "x" * 1000


async def test_drain_returns_messages_and_clears() -> None:
    """drain() returns the buffered messages decoded as strings."""
    redis_mock = _make_redis_mock(eval_returns=[b"a", b"b", b"c"])
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    messages = await buffer.drain("user-1")

    assert messages == ["a", "b", "c"]
    call_args = redis_mock.eval.await_args
    assert call_args.args[2] == "memory:turns:user-1"


async def test_drain_returns_empty_list_when_buffer_empty() -> None:
    """drain() on a missing key returns []."""
    redis_mock = _make_redis_mock(eval_returns=[])
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    messages = await buffer.drain("user-1")

    assert messages == []


async def test_drain_decodes_string_results() -> None:
    """drain() handles already-decoded string responses (decode_responses=True)."""
    redis_mock = _make_redis_mock(eval_returns=["x", "y"])
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    messages = await buffer.drain("user-1")

    assert messages == ["x", "y"]


async def test_cancel_deletes_buffer_key() -> None:
    """cancel() removes the per-user buffer key."""
    redis_mock = _make_redis_mock(eval_returns=0)
    buffer = MessageBuffer(redis=redis_mock, ttl_seconds=600)

    await buffer.cancel("user-1")

    redis_mock.delete.assert_awaited_once_with("memory:turns:user-1")
