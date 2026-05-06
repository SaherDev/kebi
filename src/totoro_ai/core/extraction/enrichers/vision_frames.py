"""Level 6 (background) — VisionFramesEnricher: extract places from video frames."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

from totoro_ai.core.config import ExtractionVisionConfig
from totoro_ai.core.extraction.types import ExtractionContext
from totoro_ai.providers.llm import VisionExtractorProtocol

logger = logging.getLogger(__name__)

_DEFAULT_VISION_CONFIG = ExtractionVisionConfig()


def _build_ffmpeg_vf(scene_threshold: float) -> str:
    """Build ffmpeg video filter: scene-change detection + bottom-third crop."""
    return rf"select=gt(scene\,{scene_threshold}),crop=iw:ih/3:0:2*ih/3"


def _split_png_frames(data: bytes) -> list[bytes]:
    """Split a concatenated PNG byte stream into individual PNG files."""
    frames: list[bytes] = []
    pos = 0
    png_header = b"\x89PNG\r\n\x1a\n"
    while pos < len(data):
        start = data.find(png_header, pos)
        if start == -1:
            break
        iend = data.find(b"IEND", start)
        if iend == -1:
            break
        end = iend + 8  # IEND chunk = 4 len + 4 "IEND" + 4 crc
        frames.append(data[start:end])
        pos = end
    return frames


class VisionFramesEnricher:
    """Level 6 background enricher — samples video frames and extracts place names.

    Uses piped subprocess chaining (yt-dlp | ffmpeg) to avoid expired CDN URL tokens.
    ADR-020: model injected via VisionExtractorProtocol — never hardcoded here.
    ADR-044: defensive prompt and image handling delegated to the extractor.
    Hard timeout: 10 seconds via asyncio.wait_for.

    Names-only producer: appends each extracted name to
    `context.known_places` and lets the deep-level finalizer
    (`LLMNEREnricher`) emit one structured `CandidatePlace` per name
    with `place_type` / `subcategory` / `cuisine` inferred from the
    name itself. Same path used by `GoogleMapsListEnricher` and
    `VisionImagesEnricher`.
    """

    def __init__(
        self,
        vision_extractor: VisionExtractorProtocol,
        config: ExtractionVisionConfig = _DEFAULT_VISION_CONFIG,
    ) -> None:
        self._vision_extractor = vision_extractor
        self._config = config
        if shutil.which("ffmpeg") is None:
            logger.warning(
                "VisionFramesEnricher: ffmpeg binary not found on PATH — "
                "vision frame extraction will be skipped. "
                "Install via: brew install ffmpeg (local) or add to nixpacks.toml (Railway)."
            )

    async def enrich(self, context: ExtractionContext) -> None:
        if not context.url:
            return
        if context.is_photo_post:
            return

        try:
            await asyncio.wait_for(
                self._run(context), timeout=self._config.timeout_seconds
            )
        except TimeoutError:
            logger.warning("VisionFramesEnricher timed out for url=%s", context.url)
        except Exception as exc:
            logger.warning(
                "VisionFramesEnricher failed for url=%s: %s", context.url, exc
            )

    async def _run(self, context: ExtractionContext) -> None:
        assert context.url is not None  # guarded in the caller
        png_bytes = await asyncio.get_running_loop().run_in_executor(
            None,
            self._capture_frames,
            context.url,
        )
        if not png_bytes:
            return

        frames = _split_png_frames(png_bytes)[: self._config.max_frames]
        if not frames:
            return

        names = await self._vision_extractor.extract_place_names(frames)
        for name in names:
            if name:
                context.known_places.append(name)

    def _capture_frames(self, url: str) -> bytes:
        """Pipe yt-dlp video stream into ffmpeg and collect PNG bytes."""
        ytdlp_proc = subprocess.Popen(
            [sys.executable, "-m", "yt_dlp", "-f", "bv", "-o", "-", url],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg",
                "-i",
                "pipe:0",
                "-vf",
                _build_ffmpeg_vf(self._config.scene_threshold),
                "-vsync",
                "vfr",
                "-frames:v",
                str(self._config.max_frames),
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "-",
            ],
            stdin=ytdlp_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if ytdlp_proc.stdout:
            ytdlp_proc.stdout.close()
        png_data, _ = ffmpeg_proc.communicate()
        ytdlp_proc.wait()
        return png_data
