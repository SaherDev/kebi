"""Tests for UserMemoryService.extract_and_save_facts (count-debounce + extract)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from totoro_ai.core.config import MemoryConfidenceConfig
from totoro_ai.core.memory.schemas import PersonalFact
from totoro_ai.core.memory.service import UserMemoryService


class _FakeBuffer:
    """In-memory MessageBuffer stand-in: tracks per-user message lists."""

    def __init__(self) -> None:
        self._store: dict[str, list[str]] = {}

    async def append(self, user_id: str, message: str) -> int:
        self._store.setdefault(user_id, []).append(message)
        return len(self._store[user_id])

    async def drain(self, user_id: str) -> list[str]:
        msgs = self._store.pop(user_id, [])
        return list(msgs)

    async def cancel(self, user_id: str) -> None:
        self._store.pop(user_id, None)


def _build_service(
    *,
    extractor_returns: list[PersonalFact] | None = None,
    debounce_messages: int = 5,
) -> tuple[UserMemoryService, MagicMock, MagicMock, _FakeBuffer]:
    repo = MagicMock()
    repo.save = AsyncMock()
    extractor = MagicMock()
    extractor.extract = AsyncMock(return_value=extractor_returns or [])
    buffer = _FakeBuffer()
    service = UserMemoryService(
        repo=repo,
        extractor=extractor,
        confidence_config=MemoryConfidenceConfig(stated=0.9, inferred=0.6),
        buffer=buffer,
        debounce_messages=debounce_messages,
    )
    return service, repo, extractor, buffer


async def test_extract_and_save_facts_buffers_below_threshold() -> None:
    service, repo, extractor, buffer = _build_service(debounce_messages=5)

    for i in range(4):
        await service.extract_and_save_facts("u1", f"msg-{i}")

    extractor.extract.assert_not_awaited()
    repo.save.assert_not_awaited()
    assert buffer._store["u1"] == ["msg-0", "msg-1", "msg-2", "msg-3"]


async def test_extract_and_save_facts_fires_on_threshold_and_drains_buffer() -> None:
    fact = PersonalFact(text="I'm vegetarian", source="stated")
    service, repo, extractor, buffer = _build_service(
        extractor_returns=[fact], debounce_messages=5
    )

    for i in range(5):
        await service.extract_and_save_facts("u1", f"msg-{i}")

    extractor.extract.assert_awaited_once()
    joined_arg = extractor.extract.await_args.args[0]
    assert joined_arg == "msg-0\nmsg-1\nmsg-2\nmsg-3\nmsg-4"
    repo.save.assert_awaited_once()
    assert "u1" not in buffer._store


async def test_extract_and_save_facts_resets_after_fire() -> None:
    """After firing on the Nth message, the next 4 messages should buffer again."""
    service, repo, extractor, _buffer = _build_service(
        extractor_returns=[PersonalFact(text="I'm vegan", source="stated")],
        debounce_messages=5,
    )

    for i in range(5):
        await service.extract_and_save_facts("u1", f"msg-{i}")
    assert extractor.extract.await_count == 1

    for i in range(4):
        await service.extract_and_save_facts("u1", f"msg-{5 + i}")

    assert extractor.extract.await_count == 1


async def test_extract_and_save_facts_skips_persistence_on_empty_facts() -> None:
    service, repo, extractor, buffer = _build_service(
        extractor_returns=[], debounce_messages=3
    )

    for i in range(3):
        await service.extract_and_save_facts("u1", f"msg-{i}")

    extractor.extract.assert_awaited_once()
    repo.save.assert_not_awaited()
    assert "u1" not in buffer._store


async def test_extract_and_save_facts_persists_with_correct_confidence() -> None:
    service, repo, _extractor, _buffer = _build_service(
        extractor_returns=[PersonalFact(text="I use a wheelchair", source="stated")],
        debounce_messages=2,
    )

    await service.extract_and_save_facts("u1", "I use a wheelchair")
    await service.extract_and_save_facts("u1", "find a place")

    repo.save.assert_awaited_once()
    kwargs = repo.save.await_args.kwargs
    assert kwargs["user_id"] == "u1"
    assert kwargs["memory"] == "I use a wheelchair"
    assert kwargs["source"] == "stated"
    assert kwargs["confidence"] == 0.9


async def test_extract_and_save_facts_no_op_when_extractor_unwired() -> None:
    """Lightweight construction (repo only) is supported for read-only test seams."""
    repo = MagicMock()
    repo.save = AsyncMock()
    service = UserMemoryService(repo=repo)

    await service.extract_and_save_facts("u1", "anything")

    repo.save.assert_not_awaited()
