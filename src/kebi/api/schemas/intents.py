"""Request/response schemas for GET /v1/user/intents (ADR-110).

The home screen's "what you wanted" list — the caller's recent intent-bearing
chat turns, played back verbatim, newest-first, keyset-paged. Tapping a row
re-submits its `text` to POST /v1/chat.

`user_id` is intentionally absent from every shape here (ADR-105): the caller's
identity arrives via the gateway header and is verified by
`require_gateway_identity`; a caller can only ever read their own intents. The
cursor is opaque on the wire — its encoding lives in one place
(`IntentCursor`). `created_at` is a raw ISO-8601 instant; relative phrasing
("yesterday, 8:42") is the client's to render (it knows the user's timezone).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.user.intent_models import IntentRecord


class IntentsQuery(BaseModel):
    """Query params for the recall-list browse endpoint."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(20, ge=1, le=100, description="Max intents per page.")
    cursor: str | None = Field(
        None,
        description=(
            "Opaque pagination cursor from a prior response's `next_cursor`. "
            "Omit for the first page."
        ),
    )


class IntentItem(BaseModel):
    """One recalled intent: the verbatim text + when it was asked."""

    id: str
    text: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: IntentRecord) -> IntentItem:
        return cls.model_validate(record, from_attributes=True)


class IntentsResponse(BaseModel):
    """One newest-first page of the caller's intents."""

    intents: list[IntentItem] = Field(
        default_factory=list, description="The recalled intents on this page."
    )
    next_cursor: str | None = Field(
        None,
        description=(
            "Opaque cursor for the next page, or null when this is the last "
            "page. An empty history returns an empty list with a null cursor."
        ),
    )

    @classmethod
    def from_page(
        cls, records: list[IntentRecord], next_cursor: str | None
    ) -> IntentsResponse:
        return cls(
            intents=[IntentItem.from_record(r) for r in records],
            next_cursor=next_cursor,
        )
