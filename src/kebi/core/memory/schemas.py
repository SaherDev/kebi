"""Personal fact Pydantic schema for user memory extraction."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Patterns the extractor LLM must not return verbatim. Any of these
# inside a "fact" is a signal that the user message tried to inject
# instructions into long-term memory rather than state a preference.
_REJECT_SUBSTRINGS = (
    "```",  # code fence / role-marker preamble
    "<|",  # OpenAI / chat-template control marker
    "system:",
    "assistant:",
    "<system>",
    "<assistant>",
)


class PersonalFact(BaseModel):
    """A declarative personal fact about the user.

    Extracted from user messages by the intent router.
    Example: "I use a wheelchair", "I'm vegetarian".

    `text` is bounded at 300 chars and stripped of newlines and role
    markers. A jailbroken extractor that emits a multi-paragraph
    instruction-shaped "fact" gets dropped at the schema boundary
    before it can land in `user_memories` and be re-injected into
    every future system prompt.
    """

    text: str = Field(min_length=1, max_length=300)
    source: Literal["stated", "inferred"]

    @field_validator("text")
    @classmethod
    def _reject_instruction_shaped(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("memory text may not contain newlines")
        lowered = v.lower()
        for marker in _REJECT_SUBSTRINGS:
            if marker in lowered:
                raise ValueError(
                    "memory text contains a role/control marker — refusing to "
                    "persist potentially instruction-shaped content"
                )
        return v.strip()
