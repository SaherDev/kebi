"""Tests for EvidenceBucketWriter.

Evidence used to ride the `/v1/extract` response — now it ships to
object storage as an append-only ledger, one JSON per (place_id,
extraction event). These tests pin the key scheme, the payload
shape, and the fail-soft behavior (storage outage must not break
the save path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.extraction.evidence_bucket import (
    EvidenceBucketReader,
    EvidenceBucketWriter,
)
from kebi.core.extraction.types import Evidence, Medium, Producer
from kebi.core.places import PlaceCategory, PlaceCore


def _place(place_id: str = "place-1") -> PlaceCore:
    return PlaceCore(
        id=place_id,
        provider_id="google:abc",
        place_name="Fuji Ramen",
        categories=[PlaceCategory.restaurant],
    )


async def test_write_puts_one_json_object_per_event() -> None:
    storage = AsyncMock()
    writer = EvidenceBucketWriter(storage=storage)
    await writer.write(
        place=_place(),
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION, snippet="ate here")],
        user_id="u-1",
        request_id="req-abc",
        source_ref="https://tiktok.com/@x/video/1",
    )
    storage.put_json.assert_awaited_once()
    key, payload = storage.put_json.await_args.args
    assert key.startswith("evidence/place-1/")
    assert key.endswith("-req-abc.json")
    assert payload["place_id"] == "place-1"
    assert payload["user_id"] == "u-1"
    assert payload["request_id"] == "req-abc"
    assert payload["source_ref"] == "https://tiktok.com/@x/video/1"
    assert payload["evidence"] == [
        {
            "producer": "llm_ner",
            "medium": "caption",
            "snippet": "ate here",
            "metadata": {},
        }
    ]


async def test_write_skips_when_place_has_no_id() -> None:
    """A candidate that wasn't persisted can't anchor a ledger entry —
    skip silently rather than write an orphaned record."""
    storage = AsyncMock()
    writer = EvidenceBucketWriter(storage=storage)
    place_no_id = PlaceCore(provider_id="google:abc", place_name="X")
    await writer.write(
        place=place_no_id,
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
        user_id="u-1",
        request_id="req-x",
        source_ref=None,
    )
    storage.put_json.assert_not_awaited()


async def test_write_skips_when_evidence_empty() -> None:
    storage = AsyncMock()
    writer = EvidenceBucketWriter(storage=storage)
    await writer.write(
        place=_place(),
        evidence=[],
        user_id="u-1",
        request_id="req-x",
        source_ref=None,
    )
    storage.put_json.assert_not_awaited()


async def test_write_swallows_storage_errors() -> None:
    """Bucket is non-critical infrastructure — a storage outage must
    not raise into the extraction save path."""
    storage = AsyncMock()
    storage.put_json = AsyncMock(side_effect=RuntimeError("s3 down"))
    writer = EvidenceBucketWriter(storage=storage)
    # No raise.
    await writer.write(
        place=_place(),
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
        user_id="u-1",
        request_id="req-x",
        source_ref=None,
    )


async def test_reader_returns_events_in_list_order() -> None:
    """Reader returns one dict per ledger key. List order is whatever
    `list_prefix` returns — for the production S3 adapter that's
    lexicographic, which coincides with chronological because keys
    begin with ISO-8601 UTC."""
    payload_a = {"recorded_at": "2026-05-25T14:22:01Z", "evidence": []}
    payload_b = {"recorded_at": "2026-05-26T09:11:30Z", "evidence": []}

    async def _list_prefix(prefix: str) -> list[str]:
        assert prefix == "evidence/place-1/"
        return [
            "evidence/place-1/2026-05-25T14:22:01Z-req_a.json",
            "evidence/place-1/2026-05-26T09:11:30Z-req_b.json",
        ]

    async def _get_json(key: str) -> dict[str, object]:
        return {
            "evidence/place-1/2026-05-25T14:22:01Z-req_a.json": payload_a,
            "evidence/place-1/2026-05-26T09:11:30Z-req_b.json": payload_b,
        }[key]

    storage = AsyncMock()
    storage.list_prefix = AsyncMock(side_effect=_list_prefix)
    storage.get_json = AsyncMock(side_effect=_get_json)

    reader = EvidenceBucketReader(storage=storage)
    events = await reader.read_for_place("place-1")
    assert events == [payload_a, payload_b]


async def test_reader_returns_empty_when_no_events_recorded() -> None:
    storage = AsyncMock()
    storage.list_prefix = AsyncMock(return_value=[])
    storage.get_json = AsyncMock()
    reader = EvidenceBucketReader(storage=storage)
    assert await reader.read_for_place("place-unknown") == []
    storage.get_json.assert_not_awaited()


async def test_reader_skips_malformed_entries() -> None:
    """A single corrupted event shouldn't blank the whole ledger —
    skip and continue so the rest is still readable."""
    storage = AsyncMock()
    storage.list_prefix = AsyncMock(
        return_value=[
            "evidence/place-1/k1.json",
            "evidence/place-1/k2.json",
        ]
    )

    async def _get_json(key: str) -> object:
        return {
            "evidence/place-1/k1.json": None,
            "evidence/place-1/k2.json": {"ok": True},
        }[key]

    storage.get_json = AsyncMock(side_effect=_get_json)
    reader = EvidenceBucketReader(storage=storage)
    events = await reader.read_for_place("place-1")
    assert events == [{"ok": True}]


async def test_two_writes_for_same_place_use_distinct_keys() -> None:
    """Append-only scheme: timestamp + request_id makes every write a
    new object. Listing by prefix later reconstructs the full ledger."""
    storage = AsyncMock()
    writer = EvidenceBucketWriter(storage=storage)
    await writer.write(
        place=_place(),
        evidence=[Evidence(Producer.LLM_NER, Medium.CAPTION)],
        user_id="u-1",
        request_id="req-A",
        source_ref=None,
    )
    await writer.write(
        place=_place(),
        evidence=[Evidence(Producer.VISION_FRAMES, Medium.FRAME)],
        user_id="u-2",
        request_id="req-B",
        source_ref=None,
    )
    assert storage.put_json.await_count == 2
    key_a = storage.put_json.await_args_list[0].args[0]
    key_b = storage.put_json.await_args_list[1].args[0]
    assert key_a != key_b
    assert key_a.startswith("evidence/place-1/")
    assert key_b.startswith("evidence/place-1/")
