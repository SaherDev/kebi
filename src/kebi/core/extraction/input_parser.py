"""Parse mixed URL + descriptive text inputs."""

import re
from dataclasses import dataclass

from kebi.core.extraction.url_source import canonicalize_url


@dataclass
class ParsedInput:
    """Result of parsing raw user input.

    `url` is the canonical form: TikTok photo→video path rewrite AND
    query/fragment strip on recognized hosts (see `canonicalize_url`).
    Two shares of the same TikTok with different `web_id` tracking
    params collapse to the same value — used as the cache key in
    ADR-074 and the `source_url` value written to `user_places`.
    """

    url: str | None  # Extracted, canonicalized URL or None
    supplementary_text: str  # All surrounding text (before + after)
    input_type: str  # "url_with_text", "url_only", "text_only"


def parse_input(raw_input: str) -> ParsedInput:
    """Parse raw user input into a canonical URL and surrounding context.

    Handles:
    - "text before https://tiktok.com/v/123 text after"
    - "https://tiktok.com/v/123 text after"
    - "text before https://tiktok.com/v/123"
    - "https://tiktok.com/v/123" (URL only)
    - "plain text description" (no URL)

    When a URL is present, it is run through `canonicalize_url` before
    being returned — the result is both yt-dlp-readable (TikTok photo
    paths rewritten to video) and stable-keyed (tracking params
    stripped on recognized hosts). The same form is used by the
    pipeline AND by the extraction result cache (ADR-074).
    """
    url_pattern = r"https?://\S+"
    match = re.search(url_pattern, raw_input)

    if not match:
        # Plain text only
        return ParsedInput(
            url=None,
            supplementary_text=raw_input.strip(),
            input_type="text_only",
        )

    url = canonicalize_url(match.group(0))

    # Extract text before and after URL (using the original substring
    # match — the canonicalized form may differ in length).
    text_before = raw_input[: match.start()].strip()
    text_after = raw_input[match.end() :].strip()
    supplementary_text = " ".join(filter(None, [text_before, text_after]))

    if supplementary_text:
        return ParsedInput(
            url=url,
            supplementary_text=supplementary_text,
            input_type="url_with_text",
        )
    else:
        return ParsedInput(
            url=url,
            supplementary_text="",
            input_type="url_only",
        )
