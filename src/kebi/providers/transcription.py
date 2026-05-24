"""Audio transcription Protocol and provider implementations (ADR-038).

Phase 4.5 subtask 2: methods return `(text, duration_seconds)` so the
caller can stamp `duration_seconds` on its tracing span. Groq Whisper
prices per-second of audio (not per-token), so duration is the metric
subtask 4's reconciliation script multiplies against the published
rate. Achieved by switching to `response_format="verbose_json"`, which
surfaces `.duration` alongside `.text`. `None` duration means the API
didn't return one (defensive — should never happen with verbose_json).
"""

from __future__ import annotations

import io
from typing import Protocol

import groq


class TranscriptionProtocol(Protocol):
    """Protocol for audio transcription providers (ADR-038)."""

    async def transcribe_url(self, cdn_url: str) -> tuple[str, float | None]:
        """Transcribe audio at cdn_url, return `(text, duration_seconds)`."""
        ...

    async def transcribe_bytes(
        self, audio_bytes: bytes, filename: str
    ) -> tuple[str, float | None]:
        """Transcribe audio bytes, return `(text, duration_seconds)`.

        Args:
            audio_bytes: Raw audio data.
            filename: Filename with extension (e.g. "audio.opus") — providers use
                      this to infer the audio format.
        """
        ...


class GroqWhisperClient(TranscriptionProtocol):
    """Groq Whisper implementation of TranscriptionProtocol."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = groq.AsyncGroq(api_key=api_key)
        self._model = model

    async def transcribe_url(self, cdn_url: str) -> tuple[str, float | None]:
        """Transcribe audio from a CDN URL without downloading."""
        response = await self._client.audio.transcriptions.create(
            model=self._model,
            file=cdn_url,  # type: ignore[arg-type]  # Groq SDK accepts URL string
            response_format="verbose_json",
        )
        duration = getattr(response, "duration", None)
        return response.text, float(duration) if duration is not None else None

    async def transcribe_bytes(
        self, audio_bytes: bytes, filename: str
    ) -> tuple[str, float | None]:
        """Transcribe audio from in-memory bytes."""
        response = await self._client.audio.transcriptions.create(
            model=self._model,
            file=(filename, io.BytesIO(audio_bytes)),
            response_format="verbose_json",
        )
        duration = getattr(response, "duration", None)
        return response.text, float(duration) if duration is not None else None
