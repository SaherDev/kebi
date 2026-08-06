"""EmbeddingService — turn PlaceCore rows into vectors and persist them.

Text builder: renders `PlaceCore` as labeled, embedder-friendly prose
(name, aliases, category, tags grouped by type, location). Underscored
enum values are flattened ("outdoor_seating" → "outdoor seating") so the
embedder sees natural phrases. Tag and alias collections are deduped and
sorted so the same logical place always hashes to the same digest —
required for the diff-then-embed path to skip unchanged rows reliably.

Diff-then-embed: every row's source text is hashed (SHA-256). Before
hitting the embedder we read the stored `(text_hash, model_name)` for
each place and skip the rows where both still match — no re-embedding,
no DB write. Saves Voyage credits and DB churn on no-op upserts.

Wiring: `EmbeddingService(repo, embedder, model_name)`. The repo
persists, the embedder produces vectors, `model_name` is stamped on
every row so the consumer can detect model drift.
"""

from __future__ import annotations

import hashlib
import logging
from enum import Enum

from .models import PlaceCore
from .protocols import EmbedderProtocol, EmbeddingsRepoProtocol

logger = logging.getLogger(__name__)


class EmbeddingService:
    def __init__(
        self,
        repo: EmbeddingsRepoProtocol,
        embedder: EmbedderProtocol,
        model_name: str,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._model_name = model_name

    async def embed_and_store(self, cores: list[PlaceCore]) -> None:
        """Embed cores whose text or model has changed and upsert the vectors.

        Cores without an `id` are skipped — there's nothing to key the
        stored vector against. Empty input is a no-op.

        Non-fatal on failure: any exception during signature lookup,
        embedder call, or upsert is logged and swallowed. The place
        rows themselves are already in the DB by the time this method
        runs (PlaceUpsertService persists before calling here), so a
        transient embedding failure — Voyage rate-limit, network blip,
        provider outage — must not block the user-visible save. The
        affected rows simply don't get embeddings this run; a later
        re-save or a backfill job picks them up via the
        diff-then-embed path.
        """
        if not cores:
            return

        usable = [c for c in cores if c.id is not None]
        if not usable:
            return

        try:
            texts = [self._build_text(c) for c in usable]
            hashes = [self._hash(t) for t in texts]

            existing = await self._repo.get_signatures_by_place_ids(
                [c.id for c in usable if c.id is not None]
            )

            # Keep only rows whose (hash, model) doesn't already match the DB.
            pending: list[tuple[str, str, str]] = []  # (place_id, text, hash)
            for core, text, h in zip(usable, texts, hashes, strict=True):
                assert core.id is not None  # filtered above
                sig = existing.get(core.id)
                if sig is not None and sig == (h, self._model_name):
                    continue
                pending.append((core.id, text, h))

            if not pending:
                return

            vectors = await self._embedder.embed(
                [text for _, text, _ in pending], input_type="document"
            )

            records = [
                (pid, vec, self._model_name, h)
                for (pid, _, h), vec in zip(pending, vectors, strict=True)
            ]
            await self._repo.upsert_embeddings(records)
        except Exception as exc:
            logger.warning(
                "embed_and_store failed (non-fatal): %s. "
                "Place rows are persisted; embeddings will be filled in on a "
                "later re-save or backfill run.",
                exc,
                exc_info=True,
                extra={
                    "place_count": len(usable),
                    "place_ids": [c.id for c in usable[:5] if c.id is not None],
                    "model_name": self._model_name,
                },
            )

    @staticmethod
    def _build_text(core: PlaceCore) -> str:
        """Render PlaceCore as deterministic, embedder-friendly prose.

        Tag and alias collections are deduped and sorted so re-saving
        the same logical place produces a byte-identical string — that's
        what lets the diff-then-embed path skip unchanged rows.

        Field list is paired with `places.search_vector` in alembic
        migration e9f0a1b2c3d4. Adding a field here means adding it
        there too, or FTS and vector recall start surfacing different
        places. tests/core/places/test_search_fields.py pins the
        contract by substring-checking both sources.
        """
        parts: list[str] = [f"Name: {core.place_name}"]

        if core.place_name_aliases:
            aliases = sorted({a.value for a in core.place_name_aliases})
            parts.append(f"Also known as: {', '.join(aliases)}")

        if core.categories:
            cats = ", ".join(_humanize(c.value) for c in core.categories)
            parts.append(f"Categories: {cats}")

        if core.tags:
            by_type: dict[str, set[str]] = {}
            for tag in core.tags:
                t = _humanize(_enum_or_str(tag.type))
                v = _humanize(_enum_or_str(tag.value))
                by_type.setdefault(t, set()).add(v)
            for type_name in sorted(by_type):
                values = sorted(by_type[type_name])
                parts.append(f"{type_name.capitalize()}: {', '.join(values)}")

        loc = core.location
        if loc:
            place_bits = [b for b in (loc.neighborhood, loc.city, loc.country) if b]
            if place_bits:
                parts.append(f"Location: {', '.join(place_bits)}")

        return "\n".join(parts)

    @staticmethod
    def _hash(text: str) -> str:
        """SHA-256 hex of the source text — the diff key for re-embed checks."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enum_or_str(v: object) -> str:
    """Return the .value of a str-based Enum, or str(v) otherwise."""
    return v.value if isinstance(v, Enum) else str(v)


def _humanize(s: str) -> str:
    """Flatten enum-style snake_case into spaced phrases for embedders."""
    return s.replace("_", " ")
