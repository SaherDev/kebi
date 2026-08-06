"""World knowledge from the open web (ADR-145).

The knowledge layer answers what kebi has been told; this answers what is
true right now and nowhere in the store yet — schedules, event dates, prices,
conditions. Findings feed the turn, and the ones that read as durable local
facts are harvested back into the claims store, so the same question is free
the second time it is asked.
"""

from __future__ import annotations

from kebi.core.web.models import WebEmptyReason, WebFinding, WebSearchResult
from kebi.core.web.service import WebKnowledgeService

__all__ = [
    "WebEmptyReason",
    "WebFinding",
    "WebKnowledgeService",
    "WebSearchResult",
]
