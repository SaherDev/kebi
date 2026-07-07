"""Process-wide httpx.AsyncClient (ADR-019)."""

from __future__ import annotations

import functools

import httpx

# Outbound call budget. Generous on the read side (15s) so Google
# Places' worst-case 5s plus a slow rehydration roundtrip still fit;
# strict on connect (5s) so we fail fast against unreachable hosts and
# don't pin workers behind half-open TCP sockets.
_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@functools.cache
def get_shared_http_client() -> httpx.AsyncClient:
    """Return the process-wide async HTTP client.

    `httpx.AsyncClient` manages a per-host connection pool internally;
    sharing one client across all outbound HTTP callers reuses that pool
    and avoids per-call TLS handshakes. Safe under uvicorn's
    one-loop-per-worker model. Closed at process exit.

    Default timeout: 15s read / 5s connect. Per-call callers can pass
    their own `timeout=` to override (the Voyage / Google clients keep
    their own tighter budgets where applicable).
    """
    return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
