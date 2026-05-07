"""User memory service — sole consumer of UserMemoryRepository (ADR-038)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.config import MemoryConfidenceConfig
from kebi.core.memory.repository import UserMemoryRepository
from kebi.core.memory.schemas import PersonalFact

if TYPE_CHECKING:
    from kebi.core.memory.buffer import MessageBuffer
    from kebi.core.memory.extractor import MemoryExtractor


class UserMemoryService:
    """Single consumer of UserMemoryRepository.

    All other components (ChatService, EventHandlers) use this service —
    never touch the repository implementation directly.

    Access constraint (ADR-038): SQLAlchemyUserMemoryRepository is instantiated
    only inside api/deps.py get_user_memory_service(). No other dependency
    function or module constructs it.

    Owns the full extraction pipeline: count-based message buffering, LLM
    extraction over the joined batch, and persistence with confidence
    scoring. Chat layer just dispatches a TurnCompleted event per turn.
    """

    def __init__(
        self,
        repo: UserMemoryRepository,
        extractor: MemoryExtractor | None = None,
        confidence_config: MemoryConfidenceConfig | None = None,
        buffer: MessageBuffer | None = None,
        debounce_messages: int = 5,
    ) -> None:
        self.repo = repo
        self._extractor = extractor
        self._confidence_config = confidence_config or MemoryConfidenceConfig()
        self._buffer = buffer
        self._debounce_messages = debounce_messages

    async def extract_and_save_facts(self, user_id: str, user_message: str) -> None:
        """Buffer the message, extract every Nth turn, persist any facts.

        No-op when the extractor or buffer is not wired (e.g. read-only
        test harnesses construct UserMemoryService with just a repo).
        """
        if self._extractor is None or self._buffer is None:
            return

        count = await self._buffer.append(user_id, user_message)
        if count < self._debounce_messages:
            return

        messages = await self._buffer.drain(user_id)
        if not messages:
            return
        joined = "\n".join(messages)

        facts = await self._extractor.extract(joined, user_id)
        if not facts:
            return

        await self.save_facts(user_id, facts, self._confidence_config)

    async def save_facts(
        self,
        user_id: str,
        facts: list[PersonalFact],
        confidence_config: MemoryConfidenceConfig,
    ) -> None:
        """Persist extracted personal facts.

        Skips write if facts list is empty.
        Assigns confidence from config by source: stated=0.9, inferred=0.6.
        Duplicate rows silently skipped by database UNIQUE constraint.

        Args:
            user_id: User identity
            facts: list of extracted PersonalFact objects
            confidence_config: config with stated and inferred thresholds
        """
        if not facts:
            return

        for fact in facts:
            confidence = (
                confidence_config.stated
                if fact.source == "stated"
                else confidence_config.inferred
            )
            await self.repo.save(
                user_id=user_id,
                memory=fact.text,
                source=fact.source,
                confidence=confidence,
            )

    async def load_memories(self, user_id: str) -> list[str]:
        """Load all stored memory strings for user_id.

        Returns [] on failure — never raises.
        Swallows repository exceptions and returns empty list.

        Args:
            user_id: User identity

        Returns:
            list[str]: Plain text memory strings, or [] on failure
        """
        try:
            return await self.repo.load(user_id)
        except Exception:
            return []
