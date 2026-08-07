"""Area profiler — a geo key's global profile from claims + geography (ADR-153).

An area reaches the screen with no row at all: its key exists only in claims
and chat links. The first time someone opens it, one LLM call dresses it —
summary prose, "best for" chips, display names for the breadcrumb, notable
children — and the result is persisted as the area's row: global, once per
area, never per user. Cost is bounded by the areas users actually open.

Input is every approved claim under the key prefix (capped) plus the model's
own geography knowledge, which is what keeps a zero-claim neighbourhood from
opening empty. Row presence is the "already profiled" guard, mirroring the
place profiler's tag check (ADR-152); a short Redis lock dedupes concurrent
first opens, and a failed call leaves no row so the next open retries.

The LLM emits *names* only — child geo keys are built mechanically from the
parent key, the same never-invent-a-key rule the chat linkifier follows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import build_geo_key
from kebi.core.places.models import normalize_icon

from .keys import display_from_slug, parent_keys
from .models import AreaChip, AreaLevel, AreaProfile, NotableSubArea

if TYPE_CHECKING:
    from kebi.core.knowledge.schemas import KnowledgeClaim
    from kebi.db.repositories.area_repository import AreaRepository
    from kebi.db.repositories.knowledge_claim_repository import (
        KnowledgeClaimRepository,
    )
    from kebi.providers.cache import CacheBackend
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)

# One profiling pass per area per lock window, however many clients open it
# at once. Not correctness-critical (the upsert is idempotent — same key,
# same global facts), purely spend control.
_LOCK_TTL_SECONDS = 300
_LOCK_KEY_PREFIX = "area_profile:inflight:"


class _ProfilerChip(BaseModel):
    icon: str | None = None
    text: str


class _ProfilerSubArea(BaseModel):
    name: str
    icon: str | None = None
    hook: str | None = None


class _ProfilerResponse(BaseModel):
    """Structured output of the profiler LLM call."""

    name: str
    level: AreaLevel
    icon: str | None = None
    summary: str
    best_for: list[_ProfilerChip] = Field(default_factory=list)
    # Ancestor display names, outermost first — one per parent key segment.
    breadcrumb: list[str] = Field(default_factory=list)
    notable_sub_areas: list[_ProfilerSubArea] = Field(default_factory=list)


class AreaProfileService:
    """Profile one unprofiled area and persist its row (ADR-153)."""

    def __init__(
        self,
        instructor_client: InstructorClient,
        area_repo: AreaRepository,
        claim_repo: KnowledgeClaimRepository,
        cache: CacheBackend,
        *,
        claims_input_limit: int,
        notable_sub_areas_max: int,
    ) -> None:
        self._instructor_client = instructor_client
        self._area_repo = area_repo
        self._claim_repo = claim_repo
        self._cache = cache
        self._claims_input_limit = claims_input_limit
        self._notable_sub_areas_max = notable_sub_areas_max

    async def profile_area(self, geo_key: str) -> AreaProfile | None:
        """Run the profiling pass for `geo_key`, best-effort.

        Returns the stored profile, or None when the pass was skipped (lock
        held, row already exists) or failed — the caller never depends on
        the result; the next open of the area reads whatever exists.
        """
        lock_key = f"{_LOCK_KEY_PREFIX}{geo_key}"
        try:
            if await self._cache.get(lock_key):
                return None
            await self._cache.set(lock_key, "1", ttl=_LOCK_TTL_SECONDS)
        except Exception:  # cache down → profile anyway, worst case is a dupe
            logger.warning("area_profile lock unavailable", exc_info=True)

        try:
            if await self._area_repo.get(geo_key) is not None:
                return None
            claims = await self._claim_repo.list_under_prefix(
                geo_key, approved_only=True
            )
            # Highest-confidence claims first when the cap bites — the ones
            # most corroborated shape the profile.
            claims.sort(key=lambda c: c.confidence, reverse=True)
            response = await self._infer(geo_key, claims[: self._claims_input_limit])
            return await self._area_repo.upsert(self._to_profile(geo_key, response))
        except Exception:
            logger.warning("area_profile failed for %s", geo_key, exc_info=True)
            return None

    def _to_profile(self, geo_key: str, response: _ProfilerResponse) -> AreaProfile:
        """Resolved profile: LLM names + mechanically built child keys."""
        parts = geo_key.split("/")
        sub_areas: list[NotableSubArea] = []
        # A neighbourhood (3 segments) is the leaf — the key grammar has no
        # deeper level to point a child at, so any children the model offers
        # are dropped rather than mis-keyed.
        if len(parts) < 3:
            for sub in response.notable_sub_areas[: self._notable_sub_areas_max]:
                try:
                    child_key = (
                        build_geo_key(parts[0], sub.name)
                        if len(parts) == 1
                        else build_geo_key(parts[0], parts[1], sub.name)
                    )
                except ValueError:
                    continue
                sub_areas.append(
                    NotableSubArea(
                        geo_key=child_key,
                        name=sub.name,
                        icon=normalize_icon(sub.icon),
                        hook=sub.hook,
                    )
                )
        # One breadcrumb name per ancestor, padded from the slugs when the
        # model returned too few — the screen never renders a raw country
        # code because the model got terse.
        parents = parent_keys(geo_key)
        breadcrumb = [
            response.breadcrumb[i]
            if i < len(response.breadcrumb) and response.breadcrumb[i].strip()
            else display_from_slug(parents[i].rsplit("/", 1)[-1])
            for i in range(len(parents))
        ]
        return AreaProfile(
            geo_key=geo_key,
            name=response.name.strip() or display_from_slug(parts[-1]),
            level=response.level,
            icon=normalize_icon(response.icon),
            summary=response.summary.strip(),
            best_for=[
                AreaChip(icon=normalize_icon(c.icon), text=c.text)
                for c in response.best_for
                if c.text.strip()
            ],
            breadcrumb=breadcrumb,
            notable_sub_areas=sub_areas,
        )

    async def _infer(
        self, geo_key: str, claims: list[KnowledgeClaim]
    ) -> _ProfilerResponse:
        """One LLM call: geo key + known claims → area profile."""
        claim_lines = "\n".join(
            f"- {c.claim}" + (f" [tags: {', '.join(c.tags)}]" if c.tags else "")
            for c in claims
        )
        user_content = (
            f"geo key: {geo_key}\n"
            f"segments: {', '.join(display_from_slug(p) for p in geo_key.split('/'))}\n"
            f"known claims about this area and inside it:\n"
            f"{claim_lines or '(none)'}\n"
        )
        async with traced_call(
            "areas.profiler",
            "area_profile",
            role="area_profiler",
            extra={"geo_key": geo_key},
            standalone=True,
        ) as t:
            try:
                response = cast(
                    _ProfilerResponse,
                    await self._instructor_client.extract(
                        response_model=_ProfilerResponse,
                        messages=[
                            {"role": "system", "content": get_prompt("area_profiler")},
                            {"role": "user", "content": user_content},
                        ],
                    ),
                )
            except Exception as exc:
                t.fail(exc)
                raise
            t.output = {"chips": len(response.best_for)}
        return response
