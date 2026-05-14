"""Embedding provider factory.

Resolves configured embedder clients by role (ADR-020, ADR-038, ADR-040).
"""

import asyncio
import logging
import time
from typing import Protocol, cast, runtime_checkable

from kebi.core.config import get_config, get_env
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Voyage rate-limit circuit breaker (process-wide)
# ---------------------------------------------------------------------------
#
# The Voyage SDK retries internally via tenacity (1s → 2s → 4s → 8s) before
# raising RateLimitError, eating ~15s on every save when rate-limited. The
# breaker traps the first failure and short-circuits every subsequent call
# for `embeddings.rate_limit_cooldown_seconds`, so a rate-limited
# environment doesn't pay the retry tax per request.
#
# Only the runtime cooldown timestamp is module-global (process-wide so
# the breaker survives across distinct embedder instances). Tuning values
# (`hard_timeout_seconds`, `rate_limit_cooldown_seconds`) live in
# config/app.yaml under `embeddings:` and are read per-call.
#
# `embed_and_store` already swallows RuntimeError as non-fatal (see
# places_v2/embedding_service.py), so the breaker is purely a latency
# optimization — correctness behavior is unchanged.

_VOYAGE_COOLDOWN_UNTIL: float = 0.0


# --- Protocol ---


@runtime_checkable
class EmbedderProtocol(Protocol):
    """Protocol for embedding providers."""

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Embed a list of text strings into vectors.

        Args:
            texts: One or more text strings to embed
            input_type: "document" (place saves) or "query" (search/recall)

        Returns:
            List of embedding vectors (one per input text), each 1024-dimensional
        """
        ...


# --- Implementation ---


class VoyageEmbedder:
    """Voyage AI embedding client implementing EmbedderProtocol (ADR-040)."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        """Initialize Voyage embedder.

        Args:
            model: Model name (e.g., 'voyage-4-lite')
            api_key: Voyage API key (uses env if None)
        """
        self._model = model
        try:
            import voyageai  # noqa: PLC0415

            self._client = voyageai.AsyncClient(api_key=api_key)  # type: ignore[attr-defined]
        except Exception as e:
            logger.error("Failed to initialize voyageai client: %s", e)
            raise

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """Embed texts using Voyage with Langfuse tracing (ADR-025).

        Wrapped with a process-wide rate-limit circuit breaker. While
        the breaker is tripped, this method short-circuits and raises a
        RuntimeError immediately — no Voyage SDK call, no waiting on the
        SDK's internal `tenacity` retry chain (which costs ~15s on
        rate-limit responses). Tuning lives in `config/app.yaml` under
        `embeddings.{hard_timeout_seconds, rate_limit_cooldown_seconds}`.

        Args:
            texts: One or more text strings to embed
            input_type: "document" (place descriptions) or "query" (search)

        Returns:
            List of 1024-dimensional embedding vectors

        Raises:
            RuntimeError: If the call fails or the breaker is tripped. The
                caller (`places_v2.EmbeddingService.embed_and_store`)
                catches this and treats it as non-fatal.
        """
        if not texts:
            raise ValueError("texts cannot be empty")

        global _VOYAGE_COOLDOWN_UNTIL

        cfg = get_config().embeddings
        hard_timeout = cfg.hard_timeout_seconds
        cooldown = cfg.rate_limit_cooldown_seconds

        # 1. Fast-fail while the breaker is tripped — no SDK call, no wait.
        now = time.monotonic()
        if now < _VOYAGE_COOLDOWN_UNTIL:
            cools_in = _VOYAGE_COOLDOWN_UNTIL - now
            raise RuntimeError(
                "Voyage circuit-breaker tripped (rate-limited or timeouts); "
                f"cooling down for {cools_in:.1f}s. Skipping embed call."
            )

        tracer = get_tracing_client()
        span = tracer.generation(name="voyage_embed", model=self._model, input=texts)

        try:
            # 2. Hard timeout so the SDK's tenacity retry chain can't eat
            #    15s of latency on rate-limit responses. `hard_timeout` is
            #    enough for a normal embed call + one TCP retry; anything
            #    past that is the retry-backoff loop and not worth waiting
            #    for.
            result = await asyncio.wait_for(
                self._client.embed(
                    texts, model=self._model, input_type=input_type
                ),
                timeout=hard_timeout,
            )
            span.end()
            return cast(list[list[float]], result.embeddings)
        except TimeoutError as e:
            _VOYAGE_COOLDOWN_UNTIL = time.monotonic() + cooldown
            span.end(output={"error": "timeout"}, level="ERROR")
            logger.warning(
                "Voyage embed timed out after %.1fs; tripping circuit "
                "breaker for %.0fs. Place rows will save without embeddings.",
                hard_timeout,
                cooldown,
            )
            raise RuntimeError(
                f"Voyage embed timed out after {hard_timeout}s"
            ) from e
        except Exception as e:
            # Trip the breaker only on rate-limit signals — other errors
            # (transient network, model error) shouldn't lock everyone out.
            if _looks_like_rate_limit(e):
                _VOYAGE_COOLDOWN_UNTIL = time.monotonic() + cooldown
                logger.warning(
                    "Voyage rate-limit detected; tripping circuit breaker "
                    "for %.0fs.",
                    cooldown,
                )
            span.end(output={"error": str(e)}, level="ERROR")
            logger.error("Embedding failed: %s", e)
            raise RuntimeError(f"Failed to embed texts: {e}") from e


def _looks_like_rate_limit(exc: BaseException) -> bool:
    """Best-effort detection of Voyage rate-limit errors without importing
    `voyageai.error` at module top (keeps the import optional).
    """
    name = type(exc).__name__
    if name in ("RateLimitError", "TooManyRequestsError"):
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "rate-limit" in msg or "429" in msg


# --- Factory ---


def get_embedder() -> EmbedderProtocol:
    """Get embedder client for the configured role.

    Resolves provider and model from config/app.yaml under the 'models.embedder' key.

    Returns:
        Embedder client implementing EmbedderProtocol

    Raises:
        KeyError: If 'embedder' role not found in config
        ValueError: If provider is unsupported
    """
    role_config = get_config().models["embedder"]
    secrets = get_env()

    provider = role_config.provider
    model = role_config.model

    if provider == "voyage":
        return VoyageEmbedder(model=model, api_key=secrets.VOYAGE_API_KEY)

    raise ValueError(f"Unsupported embedding provider: {provider}")
