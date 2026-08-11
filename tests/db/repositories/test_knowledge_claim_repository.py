"""Tests for SQLAlchemyKnowledgeClaimRepository (ADR-120).

Proves the "done when": one curated claim, one harvested claim, and one
message-origin claim all persist under the same shape, with dedup and
user-scoping enforced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kebi.db.models import (
    KnowledgeEntityType,
    KnowledgeReviewStatus,
    KnowledgeSourceType,
)
from kebi.db.repositories.knowledge_claim_repository import (
    SQLAlchemyKnowledgeClaimRepository,
)


def _mock_session_factory() -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None
    factory = MagicMock(return_value=ctx)
    return factory, session


def _row(
    id_: str,
    entity_key: str,
    claim: str,
    source_type: KnowledgeSourceType,
    user_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        entity_type=KnowledgeEntityType.PLACE,
        entity_key=entity_key,
        entity_name="Café Rider",
        claim=claim,
        tags=["quiet"],
        source_type=source_type,
        source_ref=None,
        confidence=0.8,
        user_id=user_id,
        review_status=KnowledgeReviewStatus.APPROVED,
        reviewed_by=None,
        reviewed_at=None,
        agree_count=0,
        disagree_count=0,
        created_at=datetime(2026, 7, 11, tzinfo=UTC),
    )


def _scalars_result(rows: list[SimpleNamespace]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


# ---- save: dedup + three origins under one shape ---------------------------


async def test_save_curated_expert_claim_returns_id_on_insert() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("new-id",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claim_id = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Best espresso in the neighborhood, per our resident guide.",
        source_type="curated_expert",
        confidence=0.95,
    )

    assert claim_id == "new-id"
    session.commit.assert_awaited_once()


async def test_save_shared_content_claim() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("new-id-2",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claim_id = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Frequently shown with a rooftop view in posts.",
        source_type="shared_content",
        confidence=0.4,
        source_ref="https://example.com/post/123",
    )

    assert claim_id == "new-id-2"


async def test_save_user_message_claim_carries_user_id() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("new-id-3",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="The wifi was terrible when I visited.",
        source_type="user_message",
        confidence=0.6,
        user_id="user_abc",
    )

    stmt = session.execute.await_args.args[0]
    # The insert values carry the speaker's user_id — never a global row.
    assert stmt.compile().params["user_id"] == "user_abc"


async def test_save_returns_none_on_conflict() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = None  # ON CONFLICT DO NOTHING -> no row returned
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claim_id = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Best espresso in the neighborhood, per our resident guide.",
        source_type="curated_expert",
        confidence=0.95,
    )

    assert claim_id is None


# ---- list_for_entity: user-scoping never leaks -----------------------------


async def test_list_for_entity_returns_global_rows_without_user_id() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row("c1", "place:9f3c2a", "curated fact", KnowledgeSourceType.CURATED_EXPERT)
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims = await repo.list_for_entity("place:9f3c2a")

    assert [c.id for c in claims] == ["c1"]
    assert claims[0].user_id is None


async def test_list_for_entity_scoped_to_requesting_user_only() -> None:
    factory, session = _mock_session_factory()
    # Repository-level query already filters by scope; simulate the DB
    # returning only rows visible to user_abc (global + their own).
    rows = [
        _row("c1", "place:9f3c2a", "curated fact", KnowledgeSourceType.CURATED_EXPERT),
        _row(
            "c2",
            "place:9f3c2a",
            "wifi was terrible",
            KnowledgeSourceType.USER_MESSAGE,
            user_id="user_abc",
        ),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims = await repo.list_for_entity("place:9f3c2a", user_id="user_abc")

    ids = {c.id for c in claims}
    assert ids == {"c1", "c2"}
    # No row belonging to a different user could appear in this result set.
    assert all(c.user_id in (None, "user_abc") for c in claims)


async def test_list_for_entity_query_excludes_other_users() -> None:
    """The WHERE clause itself must exclude other users' scoped rows."""
    factory, session = _mock_session_factory()
    session.execute = AsyncMock(return_value=_scalars_result([]))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    await repo.list_for_entity("place:9f3c2a", user_id="user_abc")

    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "knowledge_claims.user_id" in compiled


# ---- list_for_entities: batched multi-key read (ADR-127) -------------------


async def test_list_for_entities_batches_keys_and_returns_records() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row("c1", "place:aaa", "omakase", KnowledgeSourceType.SHARED_CONTENT),
        _row("c2", "place:bbb", "great for a date", KnowledgeSourceType.KEBI_MESSAGE),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims = await repo.list_for_entities(
        ["place:aaa", "place:bbb"], user_id="user_abc", approved_only=True
    )

    assert {c.id for c in claims} == {"c1", "c2"}
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "knowledge_claims.entity_key IN" in compiled
    assert "knowledge_claims.review_status" in compiled  # approved_only applied


async def test_list_for_entities_short_circuits_on_empty_keys() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims = await repo.list_for_entities([])

    assert claims == []
    session.execute.assert_not_called()


# ---- author-scoped list + delete (curation self-management) ----------------


async def test_list_by_source_ref_pages_and_filters_on_ref() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row("c1", "ae/dubai", "fact one", KnowledgeSourceType.CURATED_EXPERT),
        _row("c2", "ae/dubai", "fact two", KnowledgeSourceType.CURATED_EXPERT),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims, next_cursor = await repo.list_by_source_ref("curator:user_abc", 5, None)

    assert [c.id for c in claims] == ["c1", "c2"]
    assert next_cursor is None  # 2 rows for limit 5 — no further page
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "knowledge_claims.source_ref" in compiled


async def test_list_by_source_ref_emits_cursor_when_more() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row("c1", "ae/dubai", "fact one", KnowledgeSourceType.CURATED_EXPERT),
        _row("c2", "ae/dubai", "fact two", KnowledgeSourceType.CURATED_EXPERT),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims, next_cursor = await repo.list_by_source_ref("curator:user_abc", 1, None)

    assert [c.id for c in claims] == ["c1"]  # limit+1 fetch trimmed back
    assert next_cursor is not None


async def test_delete_owned_requires_matching_source_ref() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = None  # no row matched (missing or not theirs)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    deleted = await repo.delete_owned("c1", "curator:user_abc")

    assert deleted is False
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "knowledge_claims.id" in compiled
    assert "knowledge_claims.source_ref" in compiled


async def test_delete_owned_true_when_row_went_away() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("c1",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    assert await repo.delete_owned("c1", "curator:user_abc") is True
    session.commit.assert_awaited_once()


# ---- list_under_prefix: geo prefix scan ------------------------------------


async def test_list_under_prefix_matches_exact_and_nested() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row(
            "g1",
            "ae/dubai",
            "known for its skyline",
            KnowledgeSourceType.CURATED_EXPERT,
        ),
        _row(
            "g2",
            "ae/dubai/jumeirah",
            "quiet at sunrise",
            KnowledgeSourceType.SHARED_CONTENT,
        ),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    claims = await repo.list_under_prefix("ae/dubai")

    assert {c.id for c in claims} == {"g1", "g2"}
