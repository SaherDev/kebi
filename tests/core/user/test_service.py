"""Tests for UserDataDeletionService — the user-data erase sweep across
AI tables, the LangGraph checkpointer, and the in-memory taste-regen
debouncer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.sql import Delete

from kebi.core.places import UserPlacesRepo
from kebi.core.taste.debounce import RegenDebouncer
from kebi.core.user.service import DataScope, UserDataDeletionService
from kebi.db.models import (
    Interaction,
    TasteModel,
    UserMemory,
)


def _build_session_factory_mock() -> tuple[MagicMock, AsyncMock]:
    """Build a session-factory mock that yields a session whose `begin()`
    is also an async context manager (matches `async with session.begin()`).
    Returns (factory, session) for assertion access.
    """
    session = AsyncMock()
    session.execute = AsyncMock()

    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_cm)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=session_cm)
    return factory, session


def _delete_targets(session: AsyncMock) -> list[str]:
    """Extract the table name targeted by each session.execute(delete(...)).

    Uses `stmt.table.name` so it works uniformly for ORM-model deletes
    (interactions/user_memories/taste_model) and the Core-Table delete
    the UserPlacesRepo emits for user_places.
    """
    targets: list[str] = []
    for call in session.execute.await_args_list:
        stmt = call.args[0]
        assert isinstance(stmt, Delete), f"expected DELETE, got {type(stmt)}"
        targets.append(stmt.table.name)
    return targets


async def test_sweep_deletes_all_user_scoped_tables() -> None:
    factory, session = _build_session_factory_mock()
    checkpointer = AsyncMock()
    debouncer = RegenDebouncer()

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    assert _delete_targets(session) == [
        Interaction.__tablename__,
        UserMemory.__tablename__,
        TasteModel.__tablename__,
        "user_places",
    ]


async def test_sweep_runs_inside_transaction() -> None:
    """All 4 deletes must execute inside the same `session.begin()` block."""
    factory, session = _build_session_factory_mock()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    session.begin.assert_called_once()
    begin_cm = session.begin.return_value
    begin_cm.__aenter__.assert_awaited_once()
    begin_cm.__aexit__.assert_awaited_once()
    assert session.execute.await_count == 4


async def test_checkpointer_adelete_thread_called_with_user_id() -> None:
    factory, _ = _build_session_factory_mock()
    checkpointer = AsyncMock()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    checkpointer.adelete_thread.assert_awaited_once_with("user_abc")


async def test_checkpointer_none_logs_warning_and_continues() -> None:
    """Tests/lifespan-skipped envs may leave checkpointer=None; sweep
    must not crash and the rest of the cleanup must still run."""
    factory, session = _build_session_factory_mock()
    debouncer = RegenDebouncer()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=None,
        regen_debouncer=debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    assert session.execute.await_count == 4
    assert "user_abc" not in debouncer._pending


async def test_debouncer_pending_task_cancelled() -> None:
    """If a regen task is pending for the user, the sweep cancels it."""
    import asyncio
    import contextlib

    factory, _ = _build_session_factory_mock()
    debouncer = RegenDebouncer()

    cb_called = AsyncMock()
    debouncer.schedule("user_abc", cb_called, delay_seconds=10.0)
    pending_task = debouncer._pending["user_abc"]

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    with contextlib.suppress(asyncio.CancelledError):
        await pending_task

    assert pending_task.cancelled() or pending_task.done()
    assert "user_abc" not in debouncer._pending
    cb_called.assert_not_awaited()


async def test_debouncer_cancel_no_pending_task_is_noop() -> None:
    """User with no pending regen — cancel_pending must be a no-op."""
    factory, _ = _build_session_factory_mock()
    debouncer = RegenDebouncer()
    assert "user_abc" not in debouncer._pending

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")  # must not raise

    assert "user_abc" not in debouncer._pending


async def test_idempotent_double_delete() -> None:
    """Calling delete_user_data twice on the same user is fine — both calls
    succeed (DB returns 0 rows on the second; checkpointer is a no-op)."""
    factory, session = _build_session_factory_mock()
    checkpointer = AsyncMock()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")
    await service.delete_user_data("user_abc")

    assert session.execute.await_count == 8
    assert checkpointer.adelete_thread.await_count == 2


async def test_sql_error_bubbles_up_for_central_handler() -> None:
    """An exception inside the transaction propagates so api/errors.py
    can map it to a 500. The transactional context manager handles
    rollback."""
    factory, session = _build_session_factory_mock()
    session.execute.side_effect = RuntimeError("simulated db failure")

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    raised = False
    try:
        await service.delete_user_data("user_abc")
    except RuntimeError:
        raised = True
    assert raised, "expected RuntimeError to propagate"


async def test_adelete_thread_transient_failure_recovers_within_retry_budget(
    monkeypatch: object,
) -> None:
    """Two flaky attempts then success — service must absorb the transient
    failures so NestJS doesn't have to retry the whole endpoint."""
    from kebi.core.user import service as service_mod

    monkeypatch.setattr(service_mod, "_CHECKPOINT_DELETE_BACKOFF_BASE_SECONDS", 0.0)  # type: ignore[attr-defined]

    factory, _ = _build_session_factory_mock()
    checkpointer = AsyncMock()
    checkpointer.adelete_thread.side_effect = [
        RuntimeError("transient psycopg blip 1"),
        RuntimeError("transient psycopg blip 2"),
        None,
    ]
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc")

    assert checkpointer.adelete_thread.await_count == 3


async def test_adelete_thread_exhausted_retries_raises(monkeypatch: object) -> None:
    """If all retries fail, raise so NestJS sees a 500 and can drive
    recovery (idempotent re-call is safe)."""
    from kebi.core.user import service as service_mod

    monkeypatch.setattr(service_mod, "_CHECKPOINT_DELETE_BACKOFF_BASE_SECONDS", 0.0)  # type: ignore[attr-defined]

    factory, _ = _build_session_factory_mock()
    checkpointer = AsyncMock()
    checkpointer.adelete_thread.side_effect = RuntimeError("persistent failure")
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    raised = False
    try:
        await service.delete_user_data("user_abc")
    except RuntimeError as exc:
        raised = True
        assert "persistent failure" in str(exc)
    assert raised, "expected RuntimeError after retry exhaustion"
    assert checkpointer.adelete_thread.await_count == 3


# ---------------------------------------------------------------------------
# Selective scopes
# ---------------------------------------------------------------------------


async def test_chat_history_scope_skips_sql_deletes() -> None:
    """`scopes={chat_history}` must NOT touch the SQL tables — only the
    LangGraph checkpoint thread + debouncer cancellation should fire."""
    factory, session = _build_session_factory_mock()
    checkpointer = AsyncMock()
    debouncer = RegenDebouncer()

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=checkpointer,
        regen_debouncer=debouncer,
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc", scopes={DataScope.chat_history})

    # Session factory was never asked for a session.
    factory.assert_not_called()
    session.execute.assert_not_called()

    # Checkpoint thread + debouncer still cleared.
    checkpointer.adelete_thread.assert_awaited_once_with("user_abc")


async def test_user_places_deleted_in_same_transaction_under_full_sweep() -> None:
    """The full sweep must call UserPlacesRepo.delete_by_user with the
    user_id, using the SAME session that ran the three ORM deletes (one
    atomic transaction — no orphaned saves if a later step fails)."""
    factory, session = _build_session_factory_mock()
    repo = AsyncMock()
    repo.delete_by_user = AsyncMock(return_value=2)
    repo_factory = MagicMock(return_value=repo)

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=repo_factory,
    )

    await service.delete_user_data("user_abc")

    repo_factory.assert_called_once_with(session)
    repo.delete_by_user.assert_awaited_once_with("user_abc")


async def test_chat_history_scope_does_not_touch_user_places() -> None:
    """`scopes={chat_history}` must not construct the UserPlacesRepo or
    issue a user_places delete — saves stay intact."""
    factory, _ = _build_session_factory_mock()
    repo_factory = MagicMock()

    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=repo_factory,
    )

    await service.delete_user_data("user_abc", scopes={DataScope.chat_history})

    repo_factory.assert_not_called()


async def test_all_scope_explicit_is_same_as_no_scope() -> None:
    """`scopes={all}` should behave identically to passing no scopes."""
    factory, session = _build_session_factory_mock()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data("user_abc", scopes={DataScope.all})

    assert _delete_targets(session) == [
        Interaction.__tablename__,
        UserMemory.__tablename__,
        TasteModel.__tablename__,
        "user_places",
    ]


async def test_scope_set_with_all_collapses_to_all() -> None:
    """Mixing `all` with other scopes still wipes everything (`all` wins)."""
    factory, session = _build_session_factory_mock()
    service = UserDataDeletionService(
        session_factory=factory,
        checkpointer=AsyncMock(),
        regen_debouncer=RegenDebouncer(),
        user_places_repo_factory=UserPlacesRepo,
    )

    await service.delete_user_data(
        "user_abc", scopes={DataScope.all, DataScope.chat_history}
    )

    assert len(_delete_targets(session)) == 4  # full sweep ran
