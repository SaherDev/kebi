"""LLM-backed extractor for declarative personal facts.

Restored after the intent-router deletion (commit be3a445, ADR-065). The
old `use_facts` branch made an Instructor call per message; this module
takes a batch of messages (joined by the memory service) and returns a
list[PersonalFact]. All exceptions are caught — extraction must never
break the request lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from kebi.core.memory.schemas import PersonalFact
from kebi.providers.tracing import TracingClient, get_tracing_client

if TYPE_CHECKING:
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Extract only declarative user facts: first-person statements about the "
    "user's own preferences, needs, or characteristics. "
    'Examples: "I use a wheelchair", "I\'m vegetarian", "I hate seafood". '
    "NEVER extract place attributes — phrasing like \"this place is "
    'wheelchair-friendly" or "the cafe has good wifi" must NOT be included. '
    "If no personal facts are present, return an empty list. "
    'All facts must have source="stated".'
)


class _FactsResponse(BaseModel):
    """Top-level response model. Instructor needs a Pydantic class, not list[...]."""

    facts: list[PersonalFact] = Field(default_factory=list)


class MemoryExtractor:
    """Single LLM call over a batch of user messages → list[PersonalFact]."""

    def __init__(
        self,
        instructor_client: InstructorClient,
        tracer: TracingClient | None = None,
    ) -> None:
        self._client = instructor_client
        self._tracer = tracer or get_tracing_client()

    async def extract(self, message: str, user_id: str) -> list[PersonalFact]:
        """Run extraction on the joined message batch.

        Returns an empty list on any failure (logged + traced). Filters out
        any non-`stated` source the LLM might emit defensively.
        """
        span = self._tracer.generation(
            name="memory_extractor",
            input={"message": message},
            user_id=user_id,
        )
        try:
            response = await self._client.extract(
                response_model=_FactsResponse,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": message},
                ],
            )
            facts = [
                fact for fact in cast(_FactsResponse, response).facts
                if fact.source == "stated"
            ]
            span.end(output={"count": len(facts)})
            return facts
        except Exception as exc:
            logger.warning("memory extraction failed: %s", exc, exc_info=True)
            span.end(output={"error": str(exc)}, level="ERROR")
            self._tracer.capture_message(
                message=f"memory_extractor error: {exc}",
                level="error",
                metadata={"user_id": user_id},
                user_id=user_id,
            )
            return []
