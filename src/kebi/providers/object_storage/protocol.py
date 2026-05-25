"""Object storage Protocol — the only surface app code depends on."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectStorageProtocol(Protocol):
    """Minimal async object-storage surface.

    Append-only by convention: `put_json` always writes a NEW key.
    Callers that want to accumulate over time pick a key scheme that
    sidesteps overwrite (timestamp + id suffix). No read-modify-write
    is exposed because none is needed yet.
    """

    async def put_json(self, key: str, payload: Any) -> None:
        """Serialize `payload` as JSON and write it at `key`."""
        ...

    async def get_json(self, key: str) -> Any | None:
        """Read the JSON object at `key`. Returns None if absent."""
        ...

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return every key starting with `prefix`, lexicographic order."""
        ...
