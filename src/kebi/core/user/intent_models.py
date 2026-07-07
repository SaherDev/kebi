"""Domain models for the "what you wanted" recall list (ADR-110)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IntentRecord(BaseModel):
    """One recalled intent — the verbatim text plus when it was asked.

    Read model returned by the repository and service; never the ORM row.
    `created_at` is a timezone-aware instant; relative phrasing
    ("yesterday, 8:42") is rendered by the client, which knows the user's
    timezone (ADR-110).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    created_at: datetime
