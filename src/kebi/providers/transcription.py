"""Audio transcription Protocol and provider implementations (ADR-038)."""

from __future__ import annotations

import io
from typing import Protocol

import groq


class TranscriptionProtocol(Protocol):
    """Protocol for audio transcription providers (ADR-038)."""

    async def transcribe_url(self, cdn_url: str) -> str:
        """Transcribe audio at cdn_url and return the transcript text."""
        ...

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str) -> str:
        """Transcribe audio bytes and return the transcript text.

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

    async def transcribe_url(self, cdn_url: str) -> str:
        """Transcribe audio from a CDN URL without downloading."""
        response = await self._client.audio.transcriptions.create(
            model=self._model,
            file=cdn_url,  # type: ignore[arg-type]  # Groq SDK accepts URL string
        )
        return response.text

    async def transcribe_bytes(self, audio_bytes: bytes, filename: str) -> str:
        """Transcribe audio from in-memory bytes."""
        response = await self._client.audio.transcriptions.create(
            model=self._model,
            file=(filename, io.BytesIO(audio_bytes)),
        )
        return response.text
