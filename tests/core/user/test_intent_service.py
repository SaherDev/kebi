"""Tests for UserIntentService — write gates + dedup (ADR-110)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kebi.core.config import UserIntentConfig
from kebi.core.user.intent_models import IntentRecord
from kebi.core.user.intent_service import UserIntentService


class _FakeRepo:
    """In-memory stand-in for UserIntentRepository."""

    def __init__(self, latest: IntentRecord | None = None) -> None:
        self.added: list[str] = []
        self._latest = latest

    async def add(self, user_id: str, text: str, metadata=None) -> None:  # type: ignore[no-untyped-def]
        self.added.append(text)

    async def latest(self, user_id: str) -> IntentRecord | None:
        return self._latest

    async def list_page(self, user_id, limit, cursor):  # type: ignore[no-untyped-def]
        return [], None

    async def delete_by_user(self, user_id: str) -> None:
        return None


def _config(**overrides: object) -> UserIntentConfig:
    base: dict[str, object] = {
        "min_words": 3,
        "stoplist": ["yes", "the second one"],
        "dedup_window_seconds": 600,
    }
    base.update(overrides)
    return UserIntentConfig(**base)  # type: ignore[arg-type]


async def test_records_intent_bearing_turn() -> None:
    repo = _FakeRepo()
    svc = UserIntentService(repo, _config())

    await svc.record_intent("u1", "ramen, no line tonight", surfaced=True)

    assert repo.added == ["ramen, no line tonight"]


async def test_skips_when_not_surfaced() -> None:
    repo = _FakeRepo()
    svc = UserIntentService(repo, _config())

    await svc.record_intent("u1", "ramen, no line tonight", surfaced=False)

    assert repo.added == []


async def test_skips_stoplist_even_when_surfaced() -> None:
    repo = _FakeRepo()
    svc = UserIntentService(repo, _config())

    await svc.record_intent("u1", "the second one", surfaced=True)
    await svc.record_intent("u1", "YES", surfaced=True)  # case-insensitive

    assert repo.added == []


async def test_skips_below_min_words() -> None:
    repo = _FakeRepo()
    svc = UserIntentService(repo, _config(min_words=3))

    await svc.record_intent("u1", "coffee now", surfaced=True)  # 2 words

    assert repo.added == []


async def test_stores_trimmed_original_casing() -> None:
    repo = _FakeRepo()
    svc = UserIntentService(repo, _config())

    await svc.record_intent("u1", "  Drinks Somewhere Chill  ", surfaced=True)

    assert repo.added == ["Drinks Somewhere Chill"]


async def test_dedup_recent_duplicate_skipped() -> None:
    recent = IntentRecord(
        id="i1",
        text="ramen no line",
        created_at=datetime.now(UTC) - timedelta(seconds=60),
    )
    repo = _FakeRepo(latest=recent)
    svc = UserIntentService(repo, _config(dedup_window_seconds=600))

    # Same normalized text within the window → skipped.
    await svc.record_intent("u1", "Ramen No Line", surfaced=True)

    assert repo.added == []


async def test_dedup_outside_window_recorded() -> None:
    old = IntentRecord(
        id="i1",
        text="ramen no line",
        created_at=datetime.now(UTC) - timedelta(seconds=1200),
    )
    repo = _FakeRepo(latest=old)
    svc = UserIntentService(repo, _config(dedup_window_seconds=600))

    await svc.record_intent("u1", "ramen no line", surfaced=True)

    assert repo.added == ["ramen no line"]


async def test_dedup_different_text_recorded() -> None:
    recent = IntentRecord(
        id="i1",
        text="ramen no line",
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    repo = _FakeRepo(latest=recent)
    svc = UserIntentService(repo, _config())

    await svc.record_intent("u1", "dessert walking distance", surfaced=True)

    assert repo.added == ["dessert walking distance"]
