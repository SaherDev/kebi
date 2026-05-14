"""Level 5 (background) — WhisperAudioEnricher: transcribe audio into the transcript."""

from __future__ import annotations

import asyncio
import logging
import subprocess

from kebi.core.config import ExtractionWhisperConfig
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from kebi.providers.transcription import TranscriptionProtocol

logger = logging.getLogger(__name__)

_DEFAULT_WHISPER_CONFIG = ExtractionWhisperConfig()


class WhisperAudioEnricher:
    """Transcribes audio via Groq Whisper and writes it to `context.transcript`.

    Pure text producer — does NOT extract candidates. The deep level's
    `LLMNEREnricher` runs after this and harvests place names from the
    consolidated transcript / caption / supplementary text in a single
    LLM call.
    """

    def __init__(
        self,
        transcription_client: TranscriptionProtocol,
        config: ExtractionWhisperConfig = _DEFAULT_WHISPER_CONFIG,
    ) -> None:
        self._transcription_client = transcription_client
        self._config = config

    async def enrich(self, context: ExtractionContext) -> None:
        if context.transcript is not None:
            return
        if not context.url:
            return
        if context.is_photo_post:
            return

        try:
            await asyncio.wait_for(
                self._run(context), timeout=self._config.timeout_seconds
            )
        except TimeoutError:
            logger.warning("WhisperAudioEnricher timed out for url=%s", context.url)
        except Exception as exc:
            logger.warning(
                "WhisperAudioEnricher failed for url=%s: %s", context.url, exc
            )

    async def _run(self, context: ExtractionContext) -> None:
        transcript = await self._transcribe(context.url)  # type: ignore[arg-type]
        if transcript:
            context.transcript = transcript
            context.text_evidence.append(
                Evidence(
                    producer=Producer.WHISPER_AUDIO,
                    medium=Medium.TRANSCRIPT,
                    snippet=transcript[:200],
                )
            )

    async def _transcribe(self, url: str) -> str | None:
        try:
            cdn_url = await asyncio.get_event_loop().run_in_executor(
                None, self._get_cdn_url, url
            )
            return await self._transcription_client.transcribe_url(cdn_url)
        except Exception as tier1_exc:
            logger.debug("Whisper Tier 1 failed (%s), trying Tier 2", tier1_exc)

        try:
            audio_bytes = await asyncio.get_event_loop().run_in_executor(
                None, self._download_audio_bytes, url
            )
            # yt-dlp can exit clean with 0 bytes on URLs whose audio
            # stream isn't actually available (some TikTok photo-mode
            # variants slip past the is_photo_post guard). Sending 0
            # bytes to Groq Whisper returns 400 "file is empty".
            if not audio_bytes:
                logger.debug(
                    "Whisper Tier 2 skipped — yt-dlp returned 0 audio bytes for url=%s",
                    url,
                )
                return None
            filename = f"audio.{self._config.audio_format}"
            return await self._transcription_client.transcribe_bytes(
                audio_bytes, filename
            )
        except Exception as tier2_exc:
            logger.warning("Whisper Tier 2 also failed: %s", tier2_exc)
            return None

    def _get_cdn_url(self, url: str) -> str:
        result = subprocess.run(
            ["yt-dlp", "--get-url", "-f", "ba", url],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _download_audio_bytes(self, url: str) -> bytes:
        result = subprocess.run(
            [
                "yt-dlp",
                "-f",
                "ba",
                "-x",
                "--audio-format",
                self._config.audio_format,
                "--audio-quality",
                self._config.audio_quality,
                "-o",
                "-",
                url,
            ],
            capture_output=True,
            check=True,
        )
        return result.stdout
