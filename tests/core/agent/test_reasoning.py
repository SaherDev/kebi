"""Tests for ReasoningStep Pydantic model (feature 027 M3, ADR-075).

ADR-075 removed the recall/consult tools, so ReasoningStep no longer has
a `tool_name` field or a `source="tool"` variant — `source` is only
`agent` or `fallback`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from kebi.core.agent.reasoning import ReasoningStep


class TestReasoningStepDefaults:
    def test_user_visibility_default(self) -> None:
        s = ReasoningStep(
            step="agent.tool_decision", summary="checking", source="agent"
        )
        assert s.visibility == "user"

    def test_timestamp_auto_set(self) -> None:
        before = datetime.now(UTC)
        s = ReasoningStep(step="x", summary="y", source="agent")
        after = datetime.now(UTC)
        assert before <= s.timestamp <= after
        assert s.timestamp.tzinfo is not None

    def test_fallback_source_allowed(self) -> None:
        s = ReasoningStep(
            step="fallback", summary="...", source="fallback", visibility="debug"
        )
        assert s.source == "fallback"
        assert s.visibility == "debug"


class TestReasoningStepSourceValidation:
    def test_tool_source_rejected(self) -> None:
        """`source="tool"` is no longer a valid value (ADR-075)."""
        with pytest.raises(ValidationError):
            ReasoningStep(step="x", summary="...", source="tool")
