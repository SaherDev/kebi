"""Embedding provider factory.

Resolves configured embedder clients by role (ADR-020, ADR-038, ADR-040).
"""

import logging
from typing import Protocol, cast, runtime_checkable

from kebi.core.config import get_config, get_env
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)


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
        """Embed texts using Voyage 4-lite with Langfuse tracing (ADR-025).

        Args:
            texts: One or more text strings to embed
            input_type: "document" (place descriptions) or "query" (search)

        Returns:
            List of 1024-dimensional embedding vectors

        Raises:
            RuntimeError: If embedding call fails
        """
        if not texts:
            raise ValueError("texts cannot be empty")

        tracer = get_tracing_client()
        span = tracer.generation(name="voyage_embed", model=self._model, input=texts)

        try:
            result = await self._client.embed(
                texts, model=self._model, input_type=input_type
            )
            span.end()
            return cast(list[list[float]], result.embeddings)
        except Exception as e:
            span.end(output={"error": str(e)}, level="ERROR")
            logger.error("Embedding failed: %s", e)
            raise RuntimeError(f"Failed to embed texts: {e}") from e


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
