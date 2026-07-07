"""No-op object storage — used when bucket env vars are unset.

Lets local dev and tests run without a real bucket. Writes log at
debug level and discard; reads return empty.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NullObjectStorage:
    """Drop-in no-op adapter. Honors the ObjectStorageProtocol surface."""

    async def put_json(self, key: str, payload: Any) -> None:
        logger.debug("null_object_storage_put", extra={"key": key})

    async def get_json(self, key: str) -> Any | None:
        return None

    async def list_prefix(self, prefix: str) -> list[str]:
        return []
