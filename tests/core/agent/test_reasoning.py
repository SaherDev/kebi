"""Tests for ReasoningStep Pydantic model (feature 027 M3)."""

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

    def test_tool_name_optional_for_agent_source(self) -> None:
        s = ReasoningStep(step="agent.tool_decision", summary="...", source="agent")
        assert s.tool_name is None


class TestReasoningStepSourceToolNameConsistency:
    def test_tool_source_requires_tool_name(self) -> None:
        with pytest.raises(ValidationError, match="tool_name"):
            ReasoningStep(step="recall.mode", summary="...", source="tool")

    def test_agent_source_forbids_tool_name(self) -> None:
        with pytest.raises(ValidationError, match="forbids tool_name"):
            ReasoningStep(
                step="agent.tool_decision",
                summary="...",
                source="agent",
                tool_name="recall",
            )

    def test_fallback_source_forbids_tool_name(self) -> None:
        with pytest.raises(ValidationError, match="forbids tool_name"):
            ReasoningStep(
                step="fallback",
                summary="...",
                source="fallback",
                tool_name="consult",
            )

    def test_tool_source_with_tool_name_ok(self) -> None:
        s = ReasoningStep(
            step="recall.mode",
            summary="hybrid_search",
            source="tool",
            tool_name="recall",
            visibility="debug",
        )
        assert s.tool_name == "recall"
        assert s.visibility == "debug"


class TestConsultReasoningStepReexport:
    def test_consult_schema_reexports_same_class(self) -> None:
        """FR-024: api/schemas/consult.py re-exports the richer shape."""
        from kebi.api.schemas.consult import ReasoningStep as ConsultReasoningStep
        from kebi.core.agent.reasoning import ReasoningStep as AgentReasoningStep

        assert ConsultReasoningStep is AgentReasoningStep
