"""Tests for the area profiler (ADR-153).

The service's contract: only keys with no row get profiled; one in-flight
lock per key; claims feed the call highest-confidence first under the cap;
child keys are built mechanically from the LLM's names (never emitted by
it); breadcrumb names are padded from slugs when the model is terse; every
failure path returns None and writes no row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kebi.core.areas.models import AreaProfile
from kebi.core.areas.profile_service import (
    AreaProfileService,
    _ProfilerChip,
    _ProfilerResponse,
    _ProfilerSubArea,
)
from kebi.core.knowledge.schemas import KnowledgeClaim


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self.store[key] = value


class _FakeAreaRepo:
    def __init__(self, existing: AreaProfile | None = None) -> None:
        self.existing = existing
        self.upserts: list[AreaProfile] = []

    async def get(self, geo_key: str) -> AreaProfile | None:
        return self.existing

    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]:
        return {}

    async def upsert(self, profile: AreaProfile) -> AreaProfile:
        self.upserts.append(profile)
        return profile


def _claim(text: str, confidence: float) -> KnowledgeClaim:
    return KnowledgeClaim(
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        id=f"c-{text[:8]}",
        entity_type="neighborhood",
        entity_key="id/bali/canggu",
        entity_name="Canggu",
        claim=text,
        tags=["vibe"],
        source_type="shared_content",
        confidence=confidence,
        review_status="approved",
    )


def _response(**overrides: Any) -> _ProfilerResponse:
    base: dict[str, Any] = {
        "name": "Canggu",
        "level": "neighbourhood",
        "icon": "🏄",
        "summary": "the surf-and-laptop end of bali.",
        "best_for": [_ProfilerChip(icon="🌅", text="sunset drinks")],
        "breadcrumb": ["Indonesia", "Bali"],
        "notable_sub_areas": [],
    }
    base.update(overrides)
    return _ProfilerResponse(**base)


def _service(
    area_repo: _FakeAreaRepo,
    cache: _FakeCache,
    response: _ProfilerResponse | Exception,
    monkeypatch: pytest.MonkeyPatch,
    claims: list[KnowledgeClaim] | None = None,
    claims_input_limit: int = 30,
) -> tuple[AreaProfileService, AsyncMock, AsyncMock]:
    monkeypatch.setattr(
        "kebi.core.areas.profile_service.get_prompt", lambda name: "profile it"
    )
    extract = AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )
    client = AsyncMock(extract=extract)
    claim_repo = AsyncMock(list_under_prefix=AsyncMock(return_value=list(claims or [])))
    service = AreaProfileService(
        client,
        area_repo,
        claim_repo,
        cache,
        claims_input_limit=claims_input_limit,
        notable_sub_areas_max=6,
    )
    return service, extract, claim_repo.list_under_prefix


async def test_profiles_an_unprofiled_key_and_persists_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeAreaRepo()
    svc, extract, _ = _service(repo, _FakeCache(), _response(), monkeypatch)

    profile = await svc.profile_area("id/bali/canggu")

    assert profile is not None
    extract.assert_awaited_once()
    assert repo.upserts[0].geo_key == "id/bali/canggu"
    assert repo.upserts[0].name == "Canggu"
    assert repo.upserts[0].best_for[0].text == "sunset drinks"


async def test_an_already_profiled_key_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = AreaProfile(
        geo_key="id/bali/canggu",
        name="Canggu",
        level="neighbourhood",
        summary="already dressed",
    )
    repo = _FakeAreaRepo(existing=existing)
    svc, extract, _ = _service(repo, _FakeCache(), _response(), monkeypatch)

    assert await svc.profile_area("id/bali/canggu") is None
    extract.assert_not_awaited()
    assert repo.upserts == []


async def test_a_held_lock_skips_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _FakeCache()
    cache.store["area_profile:inflight:id/bali/canggu"] = "1"
    repo = _FakeAreaRepo()
    svc, extract, _ = _service(repo, cache, _response(), monkeypatch)

    assert await svc.profile_area("id/bali/canggu") is None
    extract.assert_not_awaited()


async def test_claims_feed_the_call_highest_confidence_first_under_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [_claim("weak claim", 0.2), _claim("strong claim", 0.9)]
    svc, extract, _ = _service(
        _FakeAreaRepo(),
        _FakeCache(),
        _response(),
        monkeypatch,
        claims=claims,
        claims_input_limit=1,
    )

    await svc.profile_area("id/bali/canggu")

    user_content = extract.await_args.kwargs["messages"][1]["content"]
    assert "strong claim" in user_content
    assert "weak claim" not in user_content


async def test_a_country_key_anchors_the_subject_against_child_heavy_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claims under a country prefix are dominated by its loudest child; the
    # call must pin the subject or the model profiles the child instead.
    svc, extract, _ = _service(
        _FakeAreaRepo(), _FakeCache(), _response(), monkeypatch
    )

    await svc.profile_area("id")

    user_content = extract.await_args.kwargs["messages"][1]["content"]
    assert "COUNTRY with ISO 3166 code 'id'" in user_content
    assert "not any place inside it" in user_content


async def test_child_keys_are_built_mechanically_from_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeAreaRepo()
    svc, _, _ = _service(
        repo,
        _FakeCache(),
        _response(
            name="Bali",
            level="region",
            breadcrumb=["Indonesia"],
            notable_sub_areas=[
                _ProfilerSubArea(name="Canggu", icon="🏄", hook="surf & laptops"),
                _ProfilerSubArea(name="Ubud", hook="jungle & yoga"),
            ],
        ),
        monkeypatch,
    )

    await svc.profile_area("id/bali")

    subs = repo.upserts[0].notable_sub_areas
    assert [s.geo_key for s in subs] == ["id/bali/canggu", "id/bali/ubud"]
    assert subs[0].hook == "surf & laptops"


async def test_a_leaf_key_stores_no_children_however_many_the_model_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeAreaRepo()
    svc, _, _ = _service(
        repo,
        _FakeCache(),
        _response(notable_sub_areas=[_ProfilerSubArea(name="Batu Bolong")]),
        monkeypatch,
    )

    await svc.profile_area("id/bali/canggu")

    assert repo.upserts[0].notable_sub_areas == []


async def test_breadcrumb_is_padded_from_slugs_when_the_model_is_terse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeAreaRepo()
    svc, _, _ = _service(repo, _FakeCache(), _response(breadcrumb=[]), monkeypatch)

    await svc.profile_area("id/bali/canggu")

    assert repo.upserts[0].breadcrumb == ["ID", "Bali"]


async def test_a_failed_call_writes_nothing_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeAreaRepo()
    svc, _, _ = _service(repo, _FakeCache(), RuntimeError("llm down"), monkeypatch)

    assert await svc.profile_area("id/bali/canggu") is None
    assert repo.upserts == []
