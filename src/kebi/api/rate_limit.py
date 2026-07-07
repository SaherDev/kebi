"""Per-user rate-limiter wired into the protected routes.

slowapi keys limits by the value the `key_func` returns for a given
request. We key by `user:{user_id}` after the gateway dep has stashed
the identity on `request.state.identity`. Each protected route applies
its own bucket via `@limiter.limit("...")` — see `api/routes/*.py`.

Limits exist to bound cost-per-user on LLM- and provider-heavy paths,
not to police product behavior. They are intentionally loose for normal
use and tight on the abuse case.
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter


def _rate_limit_key(request: Request) -> str:
    identity = getattr(request.state, "identity", None)
    if identity is not None:
        return f"user:{identity.user_id}"
    client = request.client
    return f"ip:{client.host}" if client is not None else "ip:unknown"


limiter = Limiter(key_func=_rate_limit_key)
