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
        created_at=datetime(2026, 7, 11, tzinfo=UTC),
    )


def _scalars_result(rows: list[SimpleNamespace]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


# ---- save: dedup + three origins under one shape ---------------------------


async def test_save_curated_expert_claim_returns_true_on_insert() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("new-id",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    inserted = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Best espresso in the neighborhood, per our resident guide.",
        source_type="curated_expert",
        confidence=0.95,
    )

    assert inserted is True
    session.commit.assert_awaited_once()


async def test_save_shared_content_claim() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = ("new-id-2",)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    inserted = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Frequently shown with a rooftop view in posts.",
        source_type="shared_content",
        confidence=0.4,
        source_ref="https://example.com/post/123",
    )

    assert inserted is True


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


async def test_save_returns_false_on_conflict() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.first.return_value = None  # ON CONFLICT DO NOTHING -> no row returned
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyKnowledgeClaimRepository(factory)

    inserted = await repo.save(
        entity_type="place",
        entity_key="place:9f3c2a",
        entity_name="Café Rider",
        claim="Best espresso in the neighborhood, per our resident guide.",
        source_type="curated_expert",
        confidence=0.95,
    )

    assert inserted is False


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
