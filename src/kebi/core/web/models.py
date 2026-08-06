"""Boundary models for the web-knowledge path (ADR-145).

`WebFinding` is what an answer can actually stand on: a piece of text, where
it came from, and how old it is. `WebSearchResult` wraps the set with the
question that produced it and, when nothing came back, a reason the prompt
can turn into honesty rather than invention.

Same discipline as `ResearchResult`: an empty result is a first-class outcome
with a name, not an empty list the model has to interpret. "I couldn't check
that" and "I checked and there's nothing" are different sentences, and the
agent can only write the right one if the difference survives to it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Why a search came back with nothing usable.
#   no_provider  — no search backend configured (no API key)
#   no_results   — the provider answered, the index had nothing
#   failed       — the provider errored or timed out
WebEmptyReason = Literal["no_provider", "no_results", "failed"]


class WebFinding(BaseModel):
    """One piece of outside-world text, attributed."""

    model_config = ConfigDict(frozen=True)

    text: str
    # Publisher domain. The agent attributes in prose ("per the schedule on
    # fifa.com") rather than emitting a URL: the chat contract is text plus
    # `kebi://` links only (ADR-136), so a bare http link has nowhere to go.
    source: str | None = None
    age: str | None = None
    # Kept server-side for the claim harvest's `source_ref`; never shown to
    # the model, which cannot click it and would only spend tokens reading it.
    url: str | None = None


class WebSearchResult(BaseModel):
    """A whole search, as the tool returns it."""

    model_config = ConfigDict(frozen=True)

    query: str
    findings: list[WebFinding] = []
    empty_reason: WebEmptyReason | None = None
    # The area the search was localised to, when the turn had one. Carried so
    # the harvest can key any claim it mines to the right place without
    # re-deriving it.
    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None
