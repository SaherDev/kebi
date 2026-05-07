"""Personal fact Pydantic schema for user memory extraction."""

from typing import Literal

from pydantic import BaseModel, Field


class PersonalFact(BaseModel):
    """A declarative personal fact about the user.

    Extracted from user messages by the intent router.
    Example: "I use a wheelchair", "I'm vegetarian".
    """

    text: str = Field(min_length=1)
    source: Literal["stated", "inferred"]
