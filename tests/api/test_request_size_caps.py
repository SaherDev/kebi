"""Schema-level enforcement of the input size caps added in the 2026-05
hardening pass.

These bound LLM-token cost and memory-buffer growth at the Pydantic
boundary — no oversized payload reaches the service layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kebi.api.schemas.chat import ChatRequest
from kebi.api.schemas.extract_place import ExtractPlaceRequest


class TestChatRequestSizeCaps:
    def test_accepts_4000_chars(self) -> None:
        ChatRequest(message="x" * 4000)

    def test_rejects_4001_chars(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="x" * 4001)

    def test_rejects_empty_message(self) -> None:
        with pytest.raises(ValidationError):
            ChatRequest(message="")


class TestExtractPlaceRequestSizeCaps:
    def test_accepts_8000_chars(self) -> None:
        ExtractPlaceRequest(raw_input="x" * 8000)

    def test_rejects_8001_chars(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceRequest(raw_input="x" * 8001)

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            ExtractPlaceRequest(raw_input="")
