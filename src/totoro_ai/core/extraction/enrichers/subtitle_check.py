"""Level 2.5 — SubtitleCheckEnricher: download video subtitles into the transcript."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from totoro_ai.core.config import ExtractionSubtitleConfig
from totoro_ai.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)

logger = logging.getLogger(__name__)

_DEFAULT_SUBTITLE_CONFIG = ExtractionSubtitleConfig()

_VTT_TIMING_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_VTT_SKIP_RE = re.compile(
    r"^(WEBVTT|NOTE|STYLE|REGION|\d+$|align:|position:|line:|size:)", re.IGNORECASE
)


def _strip_vtt(raw: str) -> str:
    """Remove VTT timing markers and metadata lines; return clean transcript text."""
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _VTT_TIMING_RE.match(stripped):
            continue
        if _VTT_SKIP_RE.match(stripped):
            continue
        lines.append(stripped)
    return " ".join(lines)


class SubtitleCheckEnricher:
    """Downloads subtitles via yt-dlp and writes them to `context.transcript`.

    Pure text producer — does NOT extract candidates. The deep level's
    `LLMNEREnricher` runs after this and harvests place names from the
    consolidated transcript / caption / supplementary text in a single
    LLM call.
    """

    def __init__(
        self,
        config: ExtractionSubtitleConfig = _DEFAULT_SUBTITLE_CONFIG,
    ) -> None:
        self._config = config

    def _download_subtitles(self, url: str, subtitle_dir: Path) -> str | None:
        """Download subtitles via yt-dlp and return cleaned transcript text.

        Runs synchronously — must be called via run_in_executor.
        """
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "yt-dlp",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-format",
                self._config.format,
                "-o",
                str(subtitle_dir / "%(id)s"),
                url,
            ],
            capture_output=True,
            text=True,
        )
        vtt_files = list(subtitle_dir.glob(f"*.{self._config.format}"))
        if not vtt_files:
            return None
        vtt_path = vtt_files[0]
        try:
            return vtt_path.read_text(encoding="utf-8")
        finally:
            vtt_path.unlink(missing_ok=True)

    async def enrich(self, context: ExtractionContext) -> None:
        if not context.url or context.transcript is not None:
            return
        if context.is_photo_post:
            return

        subtitle_dir = Path(self._config.output_dir)
        loop = asyncio.get_running_loop()
        raw_vtt = await loop.run_in_executor(
            None, self._download_subtitles, context.url, subtitle_dir
        )
        if not raw_vtt:
            return

        clean_text = _strip_vtt(raw_vtt)
        if not clean_text:
            return

        context.transcript = clean_text
        context.text_evidence.append(
            Evidence(
                producer=Producer.SUBTITLE_CHECK,
                medium=Medium.TRANSCRIPT,
                snippet=clean_text[:200],
            )
        )
