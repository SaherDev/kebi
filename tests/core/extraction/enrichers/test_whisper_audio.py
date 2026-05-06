"""Tests for WhisperAudioEnricher — text_evidence on transcript writes."""

from unittest.mock import AsyncMock, patch

import pytest

from totoro_ai.core.extraction.enrichers.whisper_audio import WhisperAudioEnricher
from totoro_ai.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)


@pytest.fixture
def transcription_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def enricher(transcription_client: AsyncMock) -> WhisperAudioEnricher:
    return WhisperAudioEnricher(transcription_client=transcription_client)


class TestWhisperAudioEnricher:
    async def test_appends_text_evidence_when_transcript_written(
        self,
        enricher: WhisperAudioEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/abc", user_id="u1"
        )
        with patch.object(
            enricher, "_transcribe", AsyncMock(return_value="Loved Fuji Ramen tonight")
        ):
            await enricher.enrich(ctx)
        assert ctx.transcript == "Loved Fuji Ramen tonight"
        assert len(ctx.text_evidence) == 1
        assert ctx.text_evidence[0] == Evidence(
            producer=Producer.WHISPER_AUDIO,
            medium=Medium.TRANSCRIPT,
            snippet="Loved Fuji Ramen tonight",
        )

    async def test_no_evidence_when_transcript_already_set(
        self,
        enricher: WhisperAudioEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/abc",
            user_id="u1",
            transcript="already there",
        )
        with patch.object(enricher, "_transcribe") as t:
            await enricher.enrich(ctx)
        t.assert_not_called()
        assert ctx.text_evidence == []

    async def test_no_evidence_when_photo_post(
        self,
        enricher: WhisperAudioEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/photo",
            user_id="u1",
            is_photo_post=True,
        )
        with patch.object(enricher, "_transcribe") as t:
            await enricher.enrich(ctx)
        t.assert_not_called()
        assert ctx.text_evidence == []

    async def test_no_evidence_when_transcribe_returns_none(
        self,
        enricher: WhisperAudioEnricher,
    ) -> None:
        ctx = ExtractionContext(
            url="https://tiktok.com/v/abc", user_id="u1"
        )
        with patch.object(
            enricher, "_transcribe", AsyncMock(return_value=None)
        ):
            await enricher.enrich(ctx)
        assert ctx.transcript is None
        assert ctx.text_evidence == []
