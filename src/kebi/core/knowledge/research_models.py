"""DTOs for the research read path — the knowledge layer's agent-facing reader.

`ResearchResult` is what the `research` tool serializes into its
`ToolMessage` (and what rides `data.tool_results` to the client), so it is
an explicit DTO (ADR-105): every field that leaves the service is declared
here, and a claim's raw provenance never does — `ResearchNote.source` is the
coarse label (community / expert / kebi), already mapped by the service.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResearchEmptyReason = Literal["unresolved", "ambiguous", "no_claims", "no_topic_match"]


class ResearchNote(BaseModel):
    """One insider note answering (part of) a research question — a claim
    reduced to what research exposes.

    `id` is the underlying claim's id (a stable key, and the target a future
    agree/disagree vote will address). `tags` are from the controlled claim
    vocabulary. `source` is the coarse origin label, never the raw
    `source_type`.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    source: str
    confidence: float
    agree_count: int = 0
    disagree_count: int = 0


class ResearchResult(BaseModel):
    """The `research` tool's payload.

    Exactly one of two shapes in practice: notes for a resolved entity, or an
    `empty_reason` + `clarification` telling the agent what to ask —
    `unresolved`/`ambiguous` (the entity couldn't be confidently pinned),
    `no_claims` (nothing known about it yet), `no_topic_match` (the entity is
    known, but not this angle). The prompt binds the agent to answer only
    from `notes` and to clarify on any empty — never fabricate or broaden.
    """

    model_config = ConfigDict(frozen=True)

    entity_name: str | None = None
    entity_key: str | None = None
    notes: list[ResearchNote] = Field(default_factory=list)
    empty_reason: ResearchEmptyReason | None = None
    clarification: str | None = None
