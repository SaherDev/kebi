"""Build the evidence trail for a candidate name from pipeline state.

This is the join layer between the producer-by-producer pipeline and
the per-candidate evidence list. Producers populate
`ExtractionContext.text_evidence` (text producers — yt-dlp, Whisper,
Subtitle, oEmbed, …) and `ExtractionContext.known_places` (name
producers — vision frames, vision images, Google Maps list). When NER
(or any future candidate emitter) wants to record what backed each name
it just emitted, it calls `collect_evidence_for(name, context)` to
walk the pipeline state and assemble every matching `Evidence` item.

Kept separate from `LLMNEREnricher` because the join is not NER-specific
— a regex-based extractor or any other emitter would need the same
logic. Kept separate from `types.py` to keep that module focused on
data shapes.
"""

from __future__ import annotations

import re

from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)

_EMOJI_MARKER_RE = re.compile(r"[\U0001F4CD\U0001F4CC\U0001F5FA]")  # 📍 📌 🗺


def normalize_name(name: str) -> str:
    """Lowercase + drop punctuation. Same rule used by `dedup._normalize`
    so candidate-vs-context matching is identical to dedup grouping."""
    without_punct = re.sub(r"[^\w\s]", "", name, flags=re.UNICODE)
    return " ".join(without_punct.lower().split())


def _contains_name(haystack: str | None, name_norm: str) -> bool:
    if not haystack:
        return False
    return name_norm in normalize_name(haystack)


def _transcript_window(
    transcript: str, name: str, width: int = 200
) -> str:
    """Best-effort: a snippet centered on the first occurrence of `name`."""
    name_norm = normalize_name(name)
    text_norm = normalize_name(transcript)
    idx = text_norm.find(name_norm)
    if idx == -1:
        return transcript[:width]
    start = max(0, idx - width // 2)
    end = min(len(transcript), start + width)
    return transcript[start:end]


def collect_evidence_for(
    candidate_name: str, context: ExtractionContext
) -> list[Evidence]:
    """Build the full audit trail for a freshly-emitted candidate.

    Walks every field on `ExtractionContext` that may have contained
    the candidate name and appends one Evidence item per match. The
    output is the candidate's complete provenance:

    - `LLM_NER` evidence for each text field on the context that
      contains the name (caption / supplementary_text / transcript /
      title / location_tag / emoji_marker / matching hashtag).
    - All `text_evidence` items the upstream text producers
      contributed whose source field contains the name.
    - All `known_places` entries whose name matches.

    By invariant the result is non-empty for any name actually emitted
    by NER — otherwise the LLM wouldn't have produced it. Callers may
    fall back to a sentinel `Evidence(LLM_NER, …)` if the join returns
    nothing (rare LLM hallucination case).
    """
    name_norm = normalize_name(candidate_name)
    evidence: list[Evidence] = []

    # 1) LLM_NER as producer for every text field that contains the name.
    if _contains_name(context.caption, name_norm):
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.CAPTION,
                snippet=context.caption[:200] if context.caption else None,
            )
        )
    if _contains_name(context.supplementary_text, name_norm):
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.SUPPLEMENTARY_TEXT,
                snippet=context.supplementary_text[:200],
            )
        )
    if _contains_name(context.transcript, name_norm):
        assert context.transcript is not None
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.TRANSCRIPT,
                snippet=_transcript_window(context.transcript, candidate_name),
            )
        )
    if _contains_name(context.title, name_norm):
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.TITLE,
                snippet=context.title[:200] if context.title else None,
            )
        )
    if _contains_name(context.location_tag, name_norm):
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.LOCATION_TAG,
                snippet=context.location_tag[:200]
                if context.location_tag
                else None,
            )
        )
    if context.caption and _EMOJI_MARKER_RE.search(context.caption):
        evidence.append(
            Evidence(
                producer=Producer.LLM_NER,
                medium=Medium.EMOJI_MARKER,
                snippet=context.caption[:200],
            )
        )
    for tag in context.hashtags:
        if normalize_name(tag) == name_norm:
            evidence.append(
                Evidence(
                    producer=Producer.LLM_NER,
                    medium=Medium.HASHTAG,
                    snippet=tag,
                )
            )

    # 2) Inherit text_evidence whose source field contains the name.
    for te in context.text_evidence:
        if te.medium == Medium.CAPTION and _contains_name(
            context.caption, name_norm
        ) or te.medium == Medium.TRANSCRIPT and _contains_name(
            context.transcript, name_norm
        ) or te.medium == Medium.TITLE and _contains_name(
            context.title, name_norm
        ) or te.medium == Medium.LOCATION_TAG and _contains_name(
            context.location_tag, name_norm
        ):
            evidence.append(te)
        elif te.medium == Medium.HASHTAG and te.snippet is not None:
            if normalize_name(te.snippet) == name_norm:
                evidence.append(te)
        # PHOTO_DETECTOR contributes context for any candidate the
        # photo path produced. Attach when known_places carries
        # vision_images evidence for this same candidate.
        elif te.medium == Medium.IMAGE and any(
            normalize_name(k.name) == name_norm
            and k.producer == Producer.VISION_IMAGES
            for k in context.known_places
        ):
            evidence.append(te)

    # 3) Inherit known_places entries whose name matches.
    for k in context.known_places:
        if normalize_name(k.name) == name_norm:
            evidence.append(
                Evidence(
                    producer=k.producer,
                    medium=k.medium,
                    snippet=k.snippet,
                )
            )

    return evidence
