"""Parse mixed URL + descriptive text inputs."""

import re
from dataclasses import dataclass


@dataclass
class ParsedInput:
    """Result of parsing raw user input."""

    url: str | None  # Extracted URL or None
    supplementary_text: str  # All surrounding text (before + after)
    input_type: str  # "url_with_text", "url_only", "text_only"


def parse_input(raw_input: str) -> ParsedInput:
    """Parse raw user input into URL and context.

    Handles:
    - "text before https://tiktok.com/v/123 text after"
    - "https://tiktok.com/v/123 text after"
    - "text before https://tiktok.com/v/123"
    - "https://tiktok.com/v/123" (URL only)
    - "plain text description" (no URL)

    Args:
        raw_input: Raw user input string

    Returns:
        ParsedInput with url, supplementary_text, and input_type
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

    url = match.group(0)

    # Extract text before and after URL
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
