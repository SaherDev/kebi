"""Tests for HarvestBucketWriter/Reader over object storage (ADR-121)."""

from __future__ import annotations

from typing import Any

from kebi.core.knowledge.harvest_bucket import HarvestBucketReader, HarvestBucketWriter
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    HarvestSnapshot,
    ResolvedGeo,
)


class _FakeStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self._data: dict[str, Any] = {}
        self._fail = fail

    async def put_json(self, key: str, payload: Any) -> None:
        if self._fail:
            raise RuntimeError("bucket down")
        self._data[key] = payload

    async def get_json(self, key: str) -> Any | None:
        return self._data.get(key)

    async def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self._data if k.startswith(prefix)]


_SNAPSHOT = HarvestSnapshot(
    content=HarvestContent(caption="ramen", source_ref="u"),
    places=[
        HarvestPlace(
            place_id="p1", name="Fuji", geo=ResolvedGeo(country_code="jp", city="Tokyo")
        )
    ],
)


async def test_write_then_read_round_trips() -> None:
    storage = _FakeStorage()
    writer = HarvestBucketWriter(storage)
    reader = HarvestBucketReader(storage)

    key = await writer.write(request_id="req-1", snapshot=_SNAPSHOT)
    assert key == "harvest/req-1.json"

    got = await reader.get(key)
    assert got == _SNAPSHOT


async def test_write_failure_is_non_fatal() -> None:
    writer = HarvestBucketWriter(_FakeStorage(fail=True))
    key = await writer.write(request_id="req-1", snapshot=_SNAPSHOT)
    assert key is None  # logged and swallowed, not raised


async def test_read_missing_key_returns_none() -> None:
    reader = HarvestBucketReader(_FakeStorage())
    assert await reader.get("harvest/nope.json") is None
