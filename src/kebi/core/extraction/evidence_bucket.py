"""Append-only evidence ledger in object storage.

Evidence used to ride the `POST /v1/extract` response (`ExtractPlaceItem.evidence`),
which polluted the product-repo contract with audit-trail noise nobody consumed.
It moves here: every extraction event writes one JSON object per place under
`evidence/{place_id}/{iso8601}-{request_id}.json`. The key scheme is
append-only — concurrent extractions of the same place each get their own
key, so no read-modify-write race. List by prefix to reconstruct the
ledger for one place.

Failure is intentionally non-fatal: the bucket is an out-of-band audit
trail, not a save-path dependency. A bucket outage logs and continues —
extraction itself still saves the place.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from kebi.core.extraction.types import Evidence
from kebi.core.places import PlaceCore
from kebi.providers.object_storage import ObjectStorageProtocol

logger = logging.getLogger(__name__)


def _evidence_to_json(e: Evidence) -> dict[str, Any]:
    return {
        "producer": e.producer.value,
        "medium": e.medium.value,
        "snippet": e.snippet,
        "metadata": dict(e.metadata),
    }


def _evidence_key(place_id: str, when: datetime, request_id: str) -> str:
    iso = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"evidence/{place_id}/{iso}-{request_id}.json"


class EvidenceBucketWriter:
    """Writes one JSON object per (place, extraction event) to the bucket.

    Decoupled from `ExtractionService` so the only dep on the storage
    protocol is here — swapping providers (Railway → R2 → S3) never
    touches the service.
    """

    def __init__(self, storage: ObjectStorageProtocol) -> None:
        self._storage = storage

    async def write(
        self,
        *,
        place: PlaceCore,
        evidence: list[Evidence],
        user_id: str,
        request_id: str,
        source_ref: str | None,
    ) -> None:
        """Write one event to the bucket. Silently no-ops if `place.id` is
        unset (place wasn't persisted) or `evidence` is empty.

        Catches and logs all storage errors — the bucket is non-critical.
        """
        if not place.id or not evidence:
            return
        when = datetime.now(UTC)
        key = _evidence_key(place.id, when, request_id)
        payload = {
            "place_id": place.id,
            "provider_id": place.provider_id,
            "place_name": place.place_name,
            "user_id": user_id,
            "request_id": request_id,
            "source_ref": source_ref,
            "recorded_at": when.isoformat().replace("+00:00", "Z"),
            "evidence": [_evidence_to_json(e) for e in evidence],
        }
        try:
            await self._storage.put_json(key, payload)
        except Exception:
            logger.warning(
                "evidence_bucket_write_failed",
                extra={"key": key, "place_id": place.id},
                exc_info=True,
            )


class EvidenceBucketReader:
    """Reads the accumulated ledger for one place.

    Keys are laid out as `evidence/{place_id}/{iso8601}-{request_id}.json`,
    so `list_prefix("evidence/{place_id}/")` returns the full event list
    for that place in chronological order — listing under a single
    prefix is the only operation needed.
    """

    def __init__(self, storage: ObjectStorageProtocol) -> None:
        self._storage = storage

    async def read_for_place(self, place_id: str) -> list[dict[str, Any]]:
        """Return every recorded extraction event for `place_id`,
        chronological order (lexicographic on the ISO timestamp prefix).

        Empty list if no events exist yet. Skips entries that fail to
        deserialize rather than crashing the whole read — a corrupted
        single event shouldn't hide the rest of the ledger.
        """
        prefix = f"evidence/{place_id}/"
        keys = await self._storage.list_prefix(prefix)
        events: list[dict[str, Any]] = []
        for key in keys:
            payload = await self._storage.get_json(key)
            if isinstance(payload, dict):
                events.append(payload)
            else:
                logger.warning(
                    "evidence_bucket_read_skipped_malformed",
                    extra={"key": key},
                )
        return events
