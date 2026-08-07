"""Place profiler — experiential tags for a catalog row from identity alone.

A place that enters the catalog through the extraction pipeline gets its
experiential tags (atmosphere, service, time, …) from the classifier LLM
reading the shared post. A place that enters through the provider
write-through — every `suggest_places` pick — gets only the mechanical
Google-derived tags, so its place screen opens empty (ADR-152).

This service closes that gap lazily: the first time such a place is
actually opened, one LLM call profiles the venue from its identity (name,
location, categories) using the same structural-tag rules the classifier
already follows, and the result is persisted onto the catalog row — global,
once per place, never per user. Cost is bounded by what users actually
open, not by what the agent surfaces.

The trigger is the row's own state (no experiential tags), so the guard is
explicit and self-clearing; a short Redis lock dedupes concurrent opens of
the same place. A failed call leaves the row untouched and the next open
retries.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt

from .models import PlaceCore, PlaceTag, normalize_icon
from .tag_merge import llm_tags_to_place_tags, merge_tags
from .tags import TagType

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

    from kebi.providers.cache import CacheBackend
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)

# Tag types the LLM knowledge layer owns (ADR-118). A row carrying none of
# these is "thin": the Google mapper only ever supplies cuisine/dietary, so
# absence of every experiential type means no LLM has looked at this place.
_EXPERIENTIAL_TYPES: frozenset[str] = frozenset(
    {
        TagType.atmosphere.value,
        TagType.feature.value,
        TagType.service.value,
        TagType.time.value,
        TagType.price.value,
        TagType.season.value,
    }
)

# One profiling pass per place per lock window, however many clients open it
# at once. Not correctness-critical (the write is idempotent — same row,
# same merge), purely spend control.
_LOCK_TTL_SECONDS = 300
_LOCK_KEY_PREFIX = "place_profile:inflight:"


class _ProfilerTag(BaseModel):
    type: str
    value: str


class _ProfilerResponse(BaseModel):
    """Structured output of the profiler LLM call."""

    tags: list[_ProfilerTag] = Field(default_factory=list)
    # Single emoji for the place's identity (ADR-117), used only when the
    # row has none — an icon a previous LLM picked is never overwritten.
    icon: str | None = None


def needs_profile(core: PlaceCore) -> bool:
    """True when no LLM has ever asserted experiential tags for this row."""
    return not any(
        (tag.type.value if isinstance(tag.type, TagType) else tag.type)
        in _EXPERIENTIAL_TYPES
        for tag in core.tags
    )


class PlaceProfileService:
    """Profile one thin catalog row and persist the result (ADR-152)."""

    def __init__(
        self,
        instructor_client: InstructorClient,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        cache: CacheBackend,
    ) -> None:
        self._instructor_client = instructor_client
        self._session_factory = session_factory
        self._cache = cache

    async def profile_place(self, place_id: str) -> PlaceCore | None:
        """Run the profiling pass for `place_id`, best-effort.

        Opens its own session (runs as a background task, after the request
        session is gone). Returns the updated core, or None when the pass
        was skipped (lock held, row gone, row no longer thin) or failed —
        the caller never depends on the result; the next open of the place
        simply reads whatever state the row is in.
        """
        lock_key = f"{_LOCK_KEY_PREFIX}{place_id}"
        try:
            if await self._cache.get(lock_key):
                return None
            await self._cache.set(lock_key, "1", ttl=_LOCK_TTL_SECONDS)
        except Exception:  # cache down → profile anyway, worst case is a dupe
            logger.warning("place_profile lock unavailable", exc_info=True)

        try:
            from .places_repo import PlacesRepo

            async with self._session_factory() as session:
                repo = PlacesRepo(session)
                cores = await repo.get_by_ids([place_id])
                if not cores or not needs_profile(cores[0]):
                    return None
                core = cores[0]

                inferred, icon = await self._infer(core)
                if not inferred:
                    return None
                # Attested tags win over newly inferred ones on conflict.
                merged = merge_tags(core.tags, inferred)
                return await repo.update_enrichment(
                    place_id,
                    merged,
                    icon=normalize_icon(icon) if core.icon is None else None,
                )
        except Exception:
            logger.warning("place_profile failed for %s", place_id, exc_info=True)
            return None

    async def _infer(self, core: PlaceCore) -> tuple[list[PlaceTag], str | None]:
        """One LLM call: venue identity → experiential tags (+ icon)."""
        loc = core.location
        where = ", ".join(
            part
            for part in [
                loc.neighborhood if loc else None,
                loc.city if loc else None,
                loc.country if loc else None,
            ]
            if part
        )
        existing = ", ".join(
            f"{t.type.value if isinstance(t.type, TagType) else t.type}={t.value}"
            for t in core.tags
        )
        user_content = (
            f"venue: {core.place_name}\n"
            f"location: {where or 'unknown'}\n"
            f"categories: {', '.join(c.value for c in core.categories) or 'unknown'}\n"
            f"existing tags: {existing or 'none'}\n"
        )
        async with traced_call(
            "places.profiler",
            "place_profile",
            role="place_profiler",
            extra={"place_id": core.id},
            standalone=True,
        ) as t:
            try:
                response = cast(
                    _ProfilerResponse,
                    await self._instructor_client.extract(
                        response_model=_ProfilerResponse,
                        messages=[
                            {"role": "system", "content": get_prompt("place_profiler")},
                            {"role": "user", "content": user_content},
                        ],
                    ),
                )
            except Exception as exc:
                t.fail(exc)
                raise
            t.output = {"tag_count": len(response.tags)}
        # Accessibility backstop + source="llm" stamping, shared with the
        # extraction classifiers.
        return llm_tags_to_place_tags(response.tags), response.icon
