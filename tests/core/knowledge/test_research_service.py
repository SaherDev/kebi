"""Tests for ResearchService — the four-stage retrieval funnel."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from kebi.core.knowledge.research_models import ResearchResult
from kebi.core.knowledge.research_resolver import (
    ResearchEntityResolver,
    ResolvedEntity,
)
from kebi.core.knowledge.research_service import (
    ResearchRankingWeights,
    ResearchService,
)
from kebi.core.knowledge.schemas import KnowledgeClaim


def _claim(
    key: str,
    text: str,
    *,
    tags: list[str] | None = None,
    confidence: float = 0.8,
    entity_type: str = "city",
    source_type: str = "shared_content",
    claim_id: str = "c-1",
) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=claim_id,
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_key=key,
        entity_name="Da Nang",
        claim=text,
        tags=tags or [],
        source_type=source_type,  # type: ignore[arg-type]
        confidence=confidence,
        agree_count=3,
        disagree_count=1,
        created_at=datetime.now(UTC),
    )


def _resolver(entity: ResolvedEntity) -> AsyncMock:
    resolver = AsyncMock(spec=ResearchEntityResolver)
    resolver.resolve = AsyncMock(return_value=entity)
    return resolver


def _repo(
    *,
    under_prefix: list[KnowledgeClaim] | None = None,
    for_entities: list[KnowledgeClaim] | None = None,
) -> AsyncMock:
    repo = AsyncMock()
    repo.list_under_prefix = AsyncMock(return_value=under_prefix or [])
    repo.list_for_entities = AsyncMock(return_value=for_entities or [])
    return repo


def _service(
    repo: AsyncMock,
    resolver: AsyncMock,
    *,
    notes_limit: int = 10,
    topic_relevance_floor: float = 0.05,
) -> ResearchService:
    return ResearchService(
        repo,
        resolver,
        default_limit=6,
        max_limit=10,
        notes_limit=notes_limit,
        weights=ResearchRankingWeights(),
        topic_relevance_floor=topic_relevance_floor,
    )


def _city_entity(key: str = "vn/da-nang") -> ResolvedEntity:
    return ResolvedEntity(
        entity_key=key, entity_type="city", entity_name="Da Nang", confidence=1.0
    )


async def _research(service: ResearchService, **kwargs: Any) -> ResearchResult:
    defaults: dict[str, Any] = {"query": "coffee scene", "user_id": "user-1"}
    defaults.update(kwargs)
    return await service.research(**defaults)


# ---------------------------------------------------------------------------
# Stage A — resolver verdict passthrough
# ---------------------------------------------------------------------------


async def test_resolver_clarify_stops_before_any_read() -> None:
    repo = _repo()
    resolver = _resolver(
        ResolvedEntity(
            needs_clarification=True,
            clarification_reason="which country?",
            empty_reason="ambiguous",
        )
    )
    result = await _research(_service(repo, resolver))

    assert result.empty_reason == "ambiguous"
    assert result.clarification == "which country?"
    assert result.notes == []
    repo.list_under_prefix.assert_not_awaited()
    repo.list_for_entities.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stage B — per-scope reads (exact keys, approved, user-scoped)
# ---------------------------------------------------------------------------


async def test_city_scope_reads_prefix_plus_country_ancestor() -> None:
    repo = _repo(under_prefix=[_claim("vn/da-nang", "city fact")])
    service = _service(repo, _resolver(_city_entity()))

    await _research(service)

    repo.list_under_prefix.assert_awaited_once_with(
        "vn/da-nang", user_id="user-1", approved_only=True
    )
    repo.list_for_entities.assert_awaited_once_with(
        ["vn"], user_id="user-1", approved_only=True
    )


async def test_country_scope_descends_under_its_prefix() -> None:
    """DECIDED: a country question reads its cities too — most claims are
    city-scoped (ADR-124), a strict country-key read would answer nothing."""
    repo = _repo(under_prefix=[_claim("vn/da-nang", "city fact")])
    entity = ResolvedEntity(
        entity_key="vn", entity_type="country", entity_name="Vietnam", confidence=0.8
    )
    service = _service(repo, _resolver(entity))

    result = await _research(service, query="what to know")

    repo.list_under_prefix.assert_awaited_once_with(
        "vn", user_id="user-1", approved_only=True
    )
    repo.list_for_entities.assert_not_awaited()
    assert result.notes  # the city-scoped claim is reachable


async def test_neighborhood_scope_inherits_ancestors() -> None:
    repo = _repo(for_entities=[_claim("vn/da-nang/my-khe", "beach fact")])
    entity = ResolvedEntity(
        entity_key="vn/da-nang/my-khe",
        entity_type="neighborhood",
        entity_name="My Khe",
        confidence=1.0,
    )
    service = _service(repo, _resolver(entity))

    await _research(service)

    repo.list_for_entities.assert_awaited_once_with(
        ["vn/da-nang/my-khe", "vn/da-nang", "vn"],
        user_id="user-1",
        approved_only=True,
    )
    repo.list_under_prefix.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stage C — ranking
# ---------------------------------------------------------------------------


async def test_tag_match_outranks_trust_alone() -> None:
    on_topic = _claim(
        "vn/da-nang",
        "BIDV ATMs charge no foreign card fee",
        tags=["no_fee_atm"],
        confidence=0.5,
        claim_id="on-topic",
    )
    ambient = _claim(
        "vn/da-nang",
        "The riverside lights up at night",
        tags=["lively"],
        confidence=0.95,
        claim_id="ambient",
    )
    repo = _repo(under_prefix=[ambient, on_topic])
    service = _service(repo, _resolver(_city_entity()))

    result = await _research(service, query="atm fees", tags=["no_fee_atm"])

    assert [n.id for n in result.notes] == ["on-topic", "ambient"]


async def test_proximity_ranks_specific_over_ambient() -> None:
    own = _claim("vn/da-nang", "city fact", claim_id="own")
    parent = _claim("vn", "country fact", entity_type="country", claim_id="parent")
    repo = _repo(under_prefix=[own], for_entities=[parent])
    service = _service(repo, _resolver(_city_entity()))

    # No topic signal that favors either — proximity decides.
    result = await _research(service, query="tell me about")

    assert [n.id for n in result.notes] == ["own", "parent"]


async def test_notes_capped_at_limit() -> None:
    claims = [_claim("vn/da-nang", f"fact {i}", claim_id=f"c{i}") for i in range(8)]
    repo = _repo(under_prefix=[*claims])
    service = _service(repo, _resolver(_city_entity()), notes_limit=3)

    result = await _research(service, query="tell me about")

    assert len(result.notes) == 3


# ---------------------------------------------------------------------------
# Stage D — honest empties + note mapping
# ---------------------------------------------------------------------------


async def test_no_candidates_returns_no_claims_with_clarification() -> None:
    service = _service(_repo(), _resolver(_city_entity()))

    result = await _research(service)

    assert result.empty_reason == "no_claims"
    assert result.entity_name == "Da Nang"
    assert result.entity_key == "vn/da-nang"
    assert result.clarification and "Da Nang" in result.clarification


async def test_off_topic_candidates_return_no_topic_match() -> None:
    repo = _repo(
        under_prefix=[_claim("vn/da-nang", "riverside bars glow", tags=["lively"])]
    )
    service = _service(repo, _resolver(_city_entity()), topic_relevance_floor=0.1)

    result = await _research(service, query="wheelchair rental shops")

    assert result.empty_reason == "no_topic_match"
    assert result.clarification and "Da Nang" in result.clarification


async def test_broad_question_never_trips_the_topic_floor() -> None:
    """'Tell me about X' carries no topic signal — trust/proximity rank it."""
    repo = _repo(under_prefix=[_claim("vn/da-nang", "riverside bars glow")])
    service = _service(repo, _resolver(_city_entity()), topic_relevance_floor=0.99)

    result = await _research(service, query="tell me about")

    assert result.empty_reason is None
    assert result.notes


async def test_notes_carry_coarse_source_label_never_raw_provenance() -> None:
    repo = _repo(
        under_prefix=[
            _claim("vn/da-nang", "a", source_type="shared_content", claim_id="c1"),
            _claim("vn/da-nang", "b", source_type="curated_expert", claim_id="c2"),
            _claim("vn/da-nang", "c", source_type="kebi_message", claim_id="c3"),
        ]
    )
    service = _service(repo, _resolver(_city_entity()))

    result = await _research(service, query="tell me about")

    labels = {n.id: n.source for n in result.notes}
    assert labels == {"c1": "community", "c2": "expert", "c3": "kebi"}
    dumped = result.model_dump_json()
    assert "shared_content" not in dumped
    assert "curated_expert" not in dumped


async def test_note_carries_id_tags_and_tally() -> None:
    repo = _repo(
        under_prefix=[
            _claim("vn/da-nang", "cash only in the old town", tags=["cash_only"])
        ]
    )
    service = _service(repo, _resolver(_city_entity()))

    result = await _research(service, query="how to pay", tags=["cash_only"])

    note = result.notes[0]
    assert note.id == "c-1"
    assert note.tags == ["cash_only"]
    assert note.agree_count == 3
    assert note.disagree_count == 1
    assert note.text == "cash only in the old town"
