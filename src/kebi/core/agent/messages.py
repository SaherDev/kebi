"""Message-normalization helpers for the agent path.

Anthropic returns `AIMessage.content` as a list of content blocks when
the response mixes text and tool_use — e.g. `[{"type": "text", "text":
"..."}, {"type": "tool_use", ...}]`, or tool_use-only responses like
`[{"type": "tool_use", ...}]`. OpenAI returns a plain string.
`extract_text_content` normalizes both shapes into a single string so
downstream code (reasoning-step summaries, `ChatResponse.message`) can
treat `AIMessage.content` uniformly.
"""

from __future__ import annotations

from typing import Any


def extract_text_content(content: Any) -> str:
    """Flatten an `AIMessage.content` value into a string.

    Returns an empty string when the content carries no text blocks
    (e.g. tool-use-only responses).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


__all__ = ["extract_text_content"]
