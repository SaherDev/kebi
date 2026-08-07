"""Tests for the place profiler (ADR-152).

The service's contract: only thin rows (no experiential tags) get profiled;
one in-flight lock per place; the LLM's tags are converted through the
shared accessibility backstop and merged with existing tags (attested wins);
the icon is written only when the row has none; every failure path returns
None and writes nothing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kebi.core.places.models import LocationContext, PlaceCore, PlaceTag
from kebi.core.places.profile_service import (
    PlaceProfileService,
    _ProfilerResponse,
    _ProfilerTag,
    needs_profile,
)
from kebi.core.places.tags import TagType


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self.store[key] = value


class _FakeRepo:
    def __init__(self, core: PlaceCore | None) -> None:
        self.core = core
        self.enrichment_calls: list[tuple[str, list[PlaceTag], str | None]] = []

    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]:
        return [self.core] if self.core else []

    async def update_enrichment(
        self, place_id: str, tags: list[PlaceTag], icon: str | None = None
    ) -> PlaceCore | None:
        self.enrichment_calls.append((place_id, tags, icon))
        assert self.core is not None
        return self.core.model_copy(update={"tags": tags, "icon": icon})


def _key(t: PlaceTag) -> tuple[str, str]:
    return (t.type.value if isinstance(t.type, TagType) else t.type, str(t.value))


def _thin_core(**overrides: Any) -> PlaceCore:
    base: dict[str, Any] = {
        "id": "p1",
        "place_name": "oneeighty Dayclub",
        "categories": ["bar"],
        "tags": [PlaceTag(type="cuisine", value="Mediterranean", source="google")],
        "location": LocationContext(city="Uluwatu", country="Indonesia"),
    }
    base.update(overrides)
    return PlaceCore(**base)


def _service(
    repo: _FakeRepo,
    cache: _FakeCache,
    response: _ProfilerResponse | Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PlaceProfileService, AsyncMock]:
    monkeypatch.setattr("kebi.core.places.places_repo.PlacesRepo", lambda session: repo)
    monkeypatch.setattr(
        "kebi.core.places.profile_service.get_prompt", lambda name: "profile it"
    )
    extract = AsyncMock(
        side_effect=response if isinstance(response, Exception) else None,
        return_value=None if isinstance(response, Exception) else response,
    )
    client = AsyncMock(extract=extract)

    @asynccontextmanager
    async def _factory():  # type: ignore[no-untyped-def]
        yield object()

    return PlaceProfileService(client, _factory, cache), extract


def test_a_row_with_only_google_tags_is_thin() -> None:
    assert needs_profile(_thin_core()) is True


def test_a_row_with_no_tags_is_thin() -> None:
    assert needs_profile(_thin_core(tags=[])) is True


def test_any_experiential_tag_means_not_thin() -> None:
    core = _thin_core(tags=[PlaceTag(type="atmosphere", value="lively", source="llm")])
    assert needs_profile(core) is False


async def test_profiles_a_thin_row_and_persists_merged_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo(_thin_core())
    svc, extract = _service(
        repo,
        _FakeCache(),
        _ProfilerResponse(
            tags=[
                _ProfilerTag(type="atmosphere", value="lively"),
                _ProfilerTag(type="time", value="afternoon"),
            ],
            icon="🏖️",
        ),
        monkeypatch,
    )

    updated = await svc.profile_place("p1")

    assert updated is not None
    extract.assert_awaited_once()
    (place_id, tags, icon) = repo.enrichment_calls[0]
    assert place_id == "p1"
    # Existing (attested) tags first, inferred appended with source=llm.
    assert [_key(t) for t in tags] == [
        ("cuisine", "Mediterranean"),
        ("atmosphere", "lively"),
        ("time", "afternoon"),
    ]
    assert all(t.source == "llm" for t in tags[1:])
    assert icon == "🏖️"


async def test_an_existing_icon_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo(_thin_core(icon="🍹"))
    svc, _ = _service(
        repo,
        _FakeCache(),
        _ProfilerResponse(
            tags=[_ProfilerTag(type="atmosphere", value="lively")], icon="🏖️"
        ),
        monkeypatch,
    )

    await svc.profile_place("p1")

    assert repo.enrichment_calls[0][2] is None


async def test_accessibility_tags_are_dropped_by_the_shared_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo(_thin_core(tags=[]))
    svc, _ = _service(
        repo,
        _FakeCache(),
        _ProfilerResponse(
            tags=[
                _ProfilerTag(type="accessibility", value="wheelchair_entrance"),
                _ProfilerTag(type="atmosphere", value="lively"),
            ]
        ),
        monkeypatch,
    )

    await svc.profile_place("p1")

    tags = repo.enrichment_calls[0][1]
    assert [_key(t) for t in tags] == [("atmosphere", "lively")]


async def test_an_inflight_lock_skips_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    cache.store["place_profile:inflight:p1"] = "1"
    repo = _FakeRepo(_thin_core())
    svc, extract = _service(repo, cache, _ProfilerResponse(), monkeypatch)

    assert await svc.profile_place("p1") is None
    extract.assert_not_awaited()
    assert repo.enrichment_calls == []


async def test_a_no_longer_thin_row_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another opener's pass finished between dispatch and this run.
    repo = _FakeRepo(
        _thin_core(tags=[PlaceTag(type="atmosphere", value="lively", source="llm")])
    )
    svc, extract = _service(repo, _FakeCache(), _ProfilerResponse(), monkeypatch)

    assert await svc.profile_place("p1") is None
    extract.assert_not_awaited()


async def test_a_missing_row_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo(None)
    svc, extract = _service(repo, _FakeCache(), _ProfilerResponse(), monkeypatch)

    assert await svc.profile_place("ghost") is None
    extract.assert_not_awaited()


async def test_an_llm_failure_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo(_thin_core())
    svc, _ = _service(repo, _FakeCache(), RuntimeError("boom"), monkeypatch)

    assert await svc.profile_place("p1") is None
    assert repo.enrichment_calls == []


async def test_zero_inferred_tags_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeRepo(_thin_core())
    svc, _ = _service(repo, _FakeCache(), _ProfilerResponse(), monkeypatch)

    assert await svc.profile_place("p1") is None
    assert repo.enrichment_calls == []
