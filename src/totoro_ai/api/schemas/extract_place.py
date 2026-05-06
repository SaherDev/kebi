"""Pydantic schemas for the extract-place endpoint (ADR-017, ADR-018, ADR-054, ADR-063).

The envelope is a pipeline-level `ExtractPlaceResponse` carrying `status` in
`{pending, completed, failed}` and a list of `ExtractPlaceItem`s. Each item is
self-describing — a non-null `place`, a non-null `confidence`, and a per-place
`status` in `{saved, needs_review, duplicate}`. No null placeholders; pipeline
states live on the envelope only. ADR-063 documents the split.

`raw_input` carries the original user-supplied string verbatim (no trimming,
no URL canonicalization, no case-folding). Replaces the pre-M0.5 `source_url`
field on this envelope. Note: `PlaceObject.source_url` (the URL the place was
extracted from, a per-place field) is unrelated and unchanged.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from totoro_ai.core.places import PlaceObject


class EvidenceDTO(BaseModel):
    """One piece of evidence in a candidate's audit trail.

    `producer` identifies which enricher contributed (`llm_ner`,
    `vision_frames`, `ytdlp_metadata`, `whisper_audio`, ...).
    `medium` identifies where in pipeline state the evidence lived
    (`caption`, `transcript`, `frame`, `image`, `list`, ...).
    `snippet` is the actual content (truncated to 200 chars) when
    available; `metadata` carries producer-specific extras.
    """

    producer: str
    medium: str
    snippet: str | None = None
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ExtractPlaceItem(BaseModel):
    """One row in the extract response.

    Per-place outcome values:
    - "saved"        — newly written to the permanent store; confidence
                       ≥ `confident_threshold` (ADR-057).
    - "needs_review" — newly written, but confidence falls in the tentative
                       band (between `save_threshold` and
                       `confident_threshold`); UI should prompt the user to
                       confirm or delete (ADR-057).
    - "duplicate"    — already existed; `place` is the existing row.

    Pipeline-level states (`pending`, `failed`) live on the response
    envelope, never on items (ADR-063).

    `evidence` is the audit trail — every producer/medium pair that
    contributed to this candidate, in extraction order. Empty list for
    legacy callers; non-empty for any candidate produced by the
    Evidence-aware pipeline.
    """

    place: PlaceObject
    confidence: float
    status: Literal["saved", "needs_review", "duplicate"]
    evidence: list[EvidenceDTO] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0], got {v}")
        return v


class ExtractPlaceRequest(BaseModel):
    """Request body for extract-place endpoint."""

    user_id: str = Field(description="User ID (validated by NestJS)")
    raw_input: str = Field(description="TikTok URL or plain text")


FailureReason = Literal[
    "unsupported_url",
    "empty_input",
    "no_candidates",
    "all_below_threshold",
    "candidate_limit_exceeded",
    "pipeline_error",
]


class ExtractPlaceResponse(BaseModel):
    """Response body for extract-place endpoint (ADR-063).

    Invariant: `results` is empty iff `status != "completed"`.

    `failure_reason` and `failure_message` are populated only when
    `status == "failed"` so callers can surface a meaningful diagnostic
    instead of an opaque "Couldn't extract a place from that".
    """

    status: Literal["pending", "completed", "failed"]
    results: list[ExtractPlaceItem] = Field(default_factory=list)
    raw_input: str | None = None
    request_id: str | None = None
    failure_reason: FailureReason | None = None
    failure_message: str | None = None

    @model_validator(mode="after")
    def _status_results_consistency(self) -> "ExtractPlaceResponse":
        if self.status == "completed" and not self.results:
            raise ValueError("status='completed' requires non-empty results")
        if self.status != "completed" and self.results:
            raise ValueError(
                f"status={self.status!r} forbids non-empty results; "
                f"pipeline-level states carry no items"
            )
        if self.status == "failed" and self.failure_reason is None:
            raise ValueError(
                "status='failed' requires failure_reason; populate it at "
                "the failure site so callers can render a diagnostic."
            )
        if self.status != "failed" and self.failure_reason is not None:
            raise ValueError(
                f"status={self.status!r} forbids failure_reason; only "
                f"'failed' carries a reason."
            )
        return self
