"""Process-wide httpx.AsyncClient (ADR-019)."""

from __future__ import annotations

import functools

import httpx


@functools.cache
def get_shared_http_client() -> httpx.AsyncClient:
    """Return the process-wide async HTTP client.

    `httpx.AsyncClient` manages a per-host connection pool internally;
    sharing one client across all outbound HTTP callers reuses that pool
    and avoids per-call TLS handshakes. Safe under uvicorn's
    one-loop-per-worker model. Closed at process exit.
    """
    return httpx.AsyncClient()
