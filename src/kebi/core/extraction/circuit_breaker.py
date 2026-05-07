"""Circuit breaker and parallel group for enrichers."""

import asyncio
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from kebi.core.extraction.types import ExtractionContext

if TYPE_CHECKING:
    from kebi.core.extraction.protocols import Enricher


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerEnricher:
    """Wraps an Enricher with circuit breaker protection.

    Trips on exceptions only. A normal None return does NOT increment the
    failure counter. This is different from a standard circuit breaker —
    returning None is the expected enricher contract, not a failure.

    Half-open: after cooldown, allows one probe. Success closes. Failure re-opens.
    """

    def __init__(
        self,
        enricher: "Enricher",
        failure_threshold: int = 3,
        cooldown_seconds: float = 900.0,
    ) -> None:
        self._enricher = enricher
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time = 0.0

    @property
    def state(self) -> CircuitState:
        return self._state

    async def enrich(self, context: ExtractionContext) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed < self._cooldown_seconds:
                return
            self._state = CircuitState.HALF_OPEN

        try:
            await self._enricher.enrich(context)
            self._reset()
        except Exception as exc:
            logger.warning(
                "CircuitBreakerEnricher caught exception from %s: %s",
                type(self._enricher).__name__,
                exc,
            )
            self._record_failure()

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()
        if self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitState.OPEN

    def _reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0


class ParallelEnricherGroup:
    """Runs all enrichers in parallel via asyncio.gather.

    Waits for ALL to complete — no cancel-on-success logic.
    Wrap individual enrichers in CircuitBreakerEnricher before passing here.
    """

    def __init__(self, enrichers: "list[Enricher]") -> None:
        self._enrichers = enrichers

    async def enrich(self, context: ExtractionContext) -> None:
        await asyncio.gather(*(e.enrich(context) for e in self._enrichers))
