"""Level 5 (background) — WhisperAudioEnricher: transcribe audio into the transcript."""

from __future__ import annotations

import asyncio
import logging
import subprocess

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import ExtractionWhisperConfig, get_config
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from kebi.providers.transcription import TranscriptionProtocol

logger = logging.getLogger(__name__)


def _whisper_cost_for(duration_seconds: float) -> float | None:
    """Compute Whisper cost from config × audio duration. None if the
    configured model name isn't priced (defensive — production has it)."""
    rate = get_config().pricing.transcription.get("whisper_large_v3_turbo")
    if rate is None:
        return None
    return rate.cost_for(duration_seconds)

_DEFAULT_WHISPER_CONFIG = ExtractionWhisperConfig()

# Hard cap on audio payload size sent to Groq Whisper. Groq's own
# limit is 25 MB; we cap a bit below that to refuse pathological
# inputs (a multi-hour stream pulled through yt-dlp) before paying the
# transcription bill. A normal TikTok / Reel comes in under 5 MB.
_MAX_AUDIO_BYTES = 24 * 1024 * 1024


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
        transcript = await self._transcribe(context)
        if transcript:
            context.transcript = transcript
            context.text_evidence.append(
                Evidence(
                    producer=Producer.WHISPER_AUDIO,
                    medium=Medium.TRANSCRIPT,
                    snippet=transcript[:200],
                )
            )

    async def _transcribe(self, context: ExtractionContext) -> str | None:
        """Two-tier Whisper attempt with per-tier Langfuse spans.

        Phase 4.5 subtask 2: each tier opens its own
        `extraction.whisper` span so a tier-1 failure followed by a
        tier-2 success shows as two distinct spans in Langfuse — same
        per-attempt convention used by the agent orchestrator. Usage
        stays empty (Groq doesn't surface token counts); `duration_seconds`
        on span output is what subtask 4 prices per second.
        """
        url = context.url
        assert url is not None  # guarded by `enrich`
        try:
            cdn_url = await asyncio.get_event_loop().run_in_executor(
                None, self._get_cdn_url, url
            )
            async with traced_call(
                "extraction.whisper",
                "extraction",
                role="transcriber",
                user_id=context.user_id,
                extra={"tier": "cdn_url"},
            ) as t:
                text, duration = await self._transcription_client.transcribe_url(
                    cdn_url
                )
                t.output = {
                    "duration_seconds": duration,
                    "text_chars": len(text),
                }
                if duration is not None:
                    t.cost_usd = _whisper_cost_for(duration)
                return text
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
            if len(audio_bytes) > _MAX_AUDIO_BYTES:
                logger.warning(
                    "Whisper Tier 2 refused %d bytes (> %d cap) for url=%s",
                    len(audio_bytes),
                    _MAX_AUDIO_BYTES,
                    url,
                )
                return None
            filename = f"audio.{self._config.audio_format}"
            async with traced_call(
                "extraction.whisper",
                "extraction",
                role="transcriber",
                user_id=context.user_id,
                extra={"tier": "audio_bytes"},
            ) as t:
                text, duration = await self._transcription_client.transcribe_bytes(
                    audio_bytes, filename
                )
                t.output = {
                    "duration_seconds": duration,
                    "text_chars": len(text),
                }
                if duration is not None:
                    t.cost_usd = _whisper_cost_for(duration)
                return text
        except Exception as tier2_exc:
            logger.warning("Whisper Tier 2 also failed: %s", tier2_exc)
            return None

    def _get_cdn_url(self, url: str) -> str:
        result = subprocess.run(
            # `ba/b`: best audio-only, falling back to best overall.
            # TikTok often serves only muxed mp4 (no audio-only stream);
            # ffmpeg (-x below) extracts the audio track from the muxed
            # container regardless.
            ["yt-dlp", "--get-url", "-f", "ba/b", url],
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
                "ba/b",  # audio-only, else muxed (see _get_cdn_url note)
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
