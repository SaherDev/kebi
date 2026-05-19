"""Level 6 (background) — VisionFramesEnricher: extract places from video frames."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

from kebi.core.config import ExtractionVisionConfig
from kebi.core.extraction.types import (
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
)
from kebi.providers.llm import VisionExtractorProtocol

logger = logging.getLogger(__name__)

_DEFAULT_VISION_CONFIG = ExtractionVisionConfig()


def _build_ffmpeg_vf(scene_threshold: float) -> str:
    """Build ffmpeg video filter: scene-change frame selection.

    No crop — listicle/title-card posts put the place name centered or
    at the top of the frame, so the previous bottom-third crop discarded
    exactly the text we need. Vision reads text anywhere in the frame.

    Downscaled to 640px height instead: keeps the whole frame (all text
    regions) but ~10x smaller PNGs, so a multi-frame vision call stays
    inside the timeout. Overlay text is large — 640px OCRs fine.
    """
    return rf"select=gt(scene\,{scene_threshold}),scale=-2:640"


def _split_png_frames(data: bytes) -> list[bytes]:
    """Split a concatenated PNG byte stream into individual PNG files.

    Split on the PNG signature, not on a literal ``IEND`` scan: the
    bytes ``IEND`` occur by chance inside photographic IDAT data, which
    truncated a frame and skipped every subsequent one. The 8-byte PNG
    signature is collision-resistant by design and never appears in
    chunk data, so frame boundaries are exactly the signature offsets.
    """
    png_header = b"\x89PNG\r\n\x1a\n"
    starts: list[int] = []
    pos = data.find(png_header)
    while pos != -1:
        starts.append(pos)
        pos = data.find(png_header, pos + len(png_header))
    return [
        data[starts[i] : (starts[i + 1] if i + 1 < len(starts) else len(data))]
        for i in range(len(starts))
    ]


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
                context.known_places.append(
                    KnownPlace(
                        name=name,
                        producer=Producer.VISION_FRAMES,
                        medium=Medium.FRAME,
                        snippet=name,
                    )
                )

    def _capture_frames(self, url: str) -> bytes:
        """Pipe yt-dlp video stream into ffmpeg and collect PNG bytes."""
        # Prefer the smallest ≤540p rendition: overlay-text OCR needs no
        # more, and a small h264/h265 stream keeps download + ffmpeg
        # decode + the multi-frame vision call inside the timeout budget.
        # `bv*` (video-bearing, may be muxed) before `b` (best overall);
        # many TikTok videos serve only muxed mp4 so a strict `bv` errors.
        fmt = "bv*[height<=540]/b[height<=540]/bv*/b"
        ytdlp_proc = subprocess.Popen(
            [sys.executable, "-m", "yt_dlp", "-f", fmt, "-o", "-", url],
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
