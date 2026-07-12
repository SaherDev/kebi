"""Durable snapshot of a share's content for the background harvest pass.

Extraction gathers a post's caption, transcript, hashtags, and the places it
resolved, then throws all but the places away. The harvester wants that
content, but re-running the pipeline (re-downloading video, re-transcribing)
is wasteful — so the content is snapshotted to object storage on the way out
and the harvest event carries only the key. The handler reads it back and
mines it off the critical path.

Writing here is non-fatal, exactly like the evidence ledger it replaces: the
bucket is best-effort enrichment, never a save-path dependency. A bucket
outage logs and continues; extraction still saves the place. Because the
snapshot is durable, a future `list_prefix("harvest/")` sweep can reprocess
what a restart dropped — the key scheme is append-only per request.

The storage-protocol dependency lives only in this module (as it did for
evidence), so swapping providers never touches the extraction service.
"""

from __future__ import annotations

import logging

from kebi.core.knowledge.schemas import HarvestSnapshot
from kebi.providers.object_storage import ObjectStorageProtocol

logger = logging.getLogger(__name__)

_PREFIX = "harvest/"


def _harvest_key(request_id: str) -> str:
    return f"{_PREFIX}{request_id}.json"


class HarvestBucketWriter:
    """Writes one harvest snapshot per extraction event to the bucket."""

    def __init__(self, storage: ObjectStorageProtocol) -> None:
        self._storage = storage

    async def write(self, *, request_id: str, snapshot: HarvestSnapshot) -> str | None:
        """Persist the snapshot; return its bucket key, or None if the write
        failed (storage errors are caught and logged — the bucket is
        non-critical). The key is what the harvest event carries."""
        key = _harvest_key(request_id)
        try:
            await self._storage.put_json(key, snapshot.model_dump(mode="json"))
        except Exception:
            logger.warning(
                "harvest_bucket_write_failed",
                extra={"key": key, "request_id": request_id},
                exc_info=True,
            )
            return None
        return key


class HarvestBucketReader:
    """Reads a harvest snapshot back for the background handler."""

    def __init__(self, storage: ObjectStorageProtocol) -> None:
        self._storage = storage

    async def get(self, key: str) -> HarvestSnapshot | None:
        """Return the snapshot at `key`, or None if absent/malformed."""
        payload = await self._storage.get_json(key)
        if not isinstance(payload, dict):
            return None
        try:
            return HarvestSnapshot.model_validate(payload)
        except Exception:
            logger.warning(
                "harvest_bucket_read_skipped_malformed",
                extra={"key": key},
            )
            return None
