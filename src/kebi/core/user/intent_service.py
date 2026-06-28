"""UserIntentService — write + read the "what you wanted" recall list (ADR-110).

Writes are gated so the list holds only intents a user would recognize:
the agent-signal gate (`surfaced`) is the primary filter, with a cheap
word-count + stoplist + dedup backstop. Reads are a newest-first keyset page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kebi.core.config import UserIntentConfig
from kebi.core.user.intent_models import IntentRecord
from kebi.db.repositories.user_intent_repository import UserIntentRepository

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Collapse whitespace and case-fold for stoplist / dedup comparison."""
    return " ".join(text.split()).lower()


class UserIntentService:
    def __init__(
        self, repo: UserIntentRepository, config: UserIntentConfig
    ) -> None:
        self._repo = repo
        self._config = config
        self._stoplist = {_normalize(s) for s in config.stoplist}

    async def record_intent(
        self, user_id: str, text: str, *, surfaced: bool
    ) -> None:
        """Persist `text` as a recalled intent if it passes the gates.

        Silently skips non-intent turns; this is best-effort background work
        and never raises into the caller (the event handler also guards it).
        """
        if not surfaced:
            return

        normalized = _normalize(text)
        if not normalized or normalized in self._stoplist:
            return
        if len(normalized.split()) < self._config.min_words:
            return
        if await self._is_recent_duplicate(user_id, normalized):
            return

        await self._repo.add(user_id, text.strip())

    async def _is_recent_duplicate(self, user_id: str, normalized: str) -> bool:
        """True when this intent repeats the user's most recent one within the
        dedup window — suppresses clutter from rapid re-asks."""
        latest = await self._repo.latest(user_id)
        if latest is None or _normalize(latest.text) != normalized:
            return False
        age_seconds = (datetime.now(UTC) - latest.created_at).total_seconds()
        return age_seconds < self._config.dedup_window_seconds

    async def list_intents(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[IntentRecord], str | None]:
        """One newest-first page of the caller's intents + the next cursor."""
        return await self._repo.list_page(user_id, limit, cursor)
