"""Tests for CircuitBreakerEnricher and ParallelEnricherGroup."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.extraction.circuit_breaker import (
    CircuitBreakerEnricher,
    CircuitState,
    ParallelEnricherGroup,
)
from kebi.core.extraction.types import ExtractionContext


def _ctx() -> ExtractionContext:
    return ExtractionContext(url=None, user_id="u1")


def _failing_enricher() -> MagicMock:
    enricher = MagicMock()
    enricher.enrich = AsyncMock(side_effect=RuntimeError("boom"))
    return enricher


def _ok_enricher() -> MagicMock:
    enricher = MagicMock()
    enricher.enrich = AsyncMock(return_value=None)
    return enricher


class TestCircuitBreakerEnricher:
    async def test_starts_closed(self) -> None:
        cb = CircuitBreakerEnricher(_ok_enricher(), failure_threshold=3)
        assert cb.state == CircuitState.CLOSED

    async def test_opens_after_threshold_exceptions(self) -> None:
        cb = CircuitBreakerEnricher(_failing_enricher(), failure_threshold=3)
        for _ in range(3):
            await cb.enrich(_ctx())  # exceptions are swallowed, not re-raised
        assert cb.state == CircuitState.OPEN

    async def test_skips_enricher_when_open(self) -> None:
        inner = _failing_enricher()
        cb = CircuitBreakerEnricher(inner, failure_threshold=2, cooldown_seconds=9999)
        for _ in range(2):
            await cb.enrich(_ctx())  # exceptions are swallowed, not re-raised
        assert cb.state == CircuitState.OPEN
        # This call should be skipped — enricher NOT called
        await cb.enrich(_ctx())
        assert inner.enrich.call_count == 2  # still 2, not 3

    async def test_none_return_does_not_increment_failure_count(self) -> None:
        """Normal None return must NOT trip the circuit."""
        inner = _ok_enricher()
        cb = CircuitBreakerEnricher(inner, failure_threshold=3)
        for _ in range(10):
            await cb.enrich(_ctx())
        assert cb.state == CircuitState.CLOSED
        assert cb._consecutive_failures == 0

    async def test_half_open_after_cooldown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        cb = CircuitBreakerEnricher(
            _failing_enricher(), failure_threshold=2, cooldown_seconds=10
        )
        for _ in range(2):
            await cb.enrich(_ctx())  # exceptions are swallowed, not re-raised
        assert cb.state == CircuitState.OPEN

        # Fast-forward time past cooldown
        monkeypatch.setattr(time, "monotonic", lambda: cb._last_failure_time + 11)
        inner_ok = _ok_enricher()
        cb._enricher = inner_ok
        await cb.enrich(_ctx())
        assert cb.state == CircuitState.CLOSED
        inner_ok.enrich.assert_called_once()

    async def test_resets_on_success(self) -> None:
        inner = _ok_enricher()
        cb = CircuitBreakerEnricher(inner, failure_threshold=5)
        await cb.enrich(_ctx())
        assert cb._consecutive_failures == 0
        assert cb.state == CircuitState.CLOSED


class TestParallelEnricherGroup:
    async def test_runs_all_enrichers(self) -> None:
        a, b, c = _ok_enricher(), _ok_enricher(), _ok_enricher()
        group = ParallelEnricherGroup([a, b, c])
        await group.enrich(_ctx())
        a.enrich.assert_called_once()
        b.enrich.assert_called_once()
        c.enrich.assert_called_once()

    async def test_returns_none(self) -> None:
        group = ParallelEnricherGroup([_ok_enricher()])
        result = await group.enrich(_ctx())
        assert result is None
