"""Candidate-name generator for the `suggest_places` agent tool.

Asks the configured `candidate_namer` LLM (Instructor structured output)
for a list of real, well-known place names that fit a user's intent at
the turn's working location and movement profile. The output is a list
of `CandidateName(name, reason)` entries that the `suggest_places` tool
then validates against the place provider.

The service never returns place objects — only proposed names and the
reason the LLM gave for each. Validation, persistence, and constraint
filtering live in the tool.

Location anchoring is enforced by the prompt: every render must carry
city/country, search radius, and movement capabilities. The tool is
responsible for never calling `generate()` without those values resolved
— the prompt cannot enforce that the slot was populated, only that the
LLM honours it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, Field

from kebi.core.config import get_prompt
from kebi.core.places.models import PlaceCategory
from kebi.providers.tracing import TracingClient, get_tracing_client

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class CandidateName(BaseModel):
    """One proposed place name and the LLM's reason for proposing it."""

    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CandidateNames(BaseModel):
    """Top-level Instructor response — list of named candidates."""

    candidates: list[CandidateName] = Field(default_factory=list)


def _render_location_block(working: WorkingLocation) -> str:
    """Pin every namer call to the resolved working location.

    The radius + density + bbox are all derived deterministically by the
    location resolver (ADR-083/084); the LLM never recomputes — it
    receives them and must honour them.
    """
    parts: list[str] = [f"City: {working.city}", f"Country: {working.country}"]
    if working.neighborhood:
        parts.append(f"Neighborhood: {working.neighborhood}")
    parts.append(f"Coordinates: lat={working.lat:.4f}, lng={working.lng:.4f}")
    parts.append(
        f"Search radius: {int(working.search_radius_m)} metres "
        f"(density: {working.density})"
    )
    return "\n".join(parts)


def _render_mobility_block(working: WorkingLocation) -> str:
    """How the user gets around for this turn — sets reachability bounds."""
    return (
        f"Effective mode: {working.effective_mode}\n"
        f"Scope: {working.scope_tier} {working.scope_shape}"
    )


def _render_list_block(items: list[str] | None, label: str) -> str:
    """Render an optional list as a single-line block, or 'none' when absent."""
    if not items:
        return f"(none — {label} unconstrained)"
    return ", ".join(items)


def _render_taste_block(taste_summary: str | None) -> str:
    """The user's prior preferences, free text or a placeholder."""
    summary = (taste_summary or "").strip()
    if not summary:
        return "(no prior taste signal — treat as cold start)"
    return summary


class CandidateNamerService:
    """Single LLM call → list of named place candidates with reasons."""

    def __init__(
        self,
        instructor_client: InstructorClient,
        tracer: TracingClient | None = None,
    ) -> None:
        self._client = instructor_client
        self._tracer = tracer or get_tracing_client()

    async def generate(
        self,
        *,
        intent: str,
        working: WorkingLocation,
        categories: list[PlaceCategory] | None,
        tags: list[str] | None,
        taste_summary: str | None,
        count: int,
        user_id: str | None = None,
    ) -> CandidateNames:
        """Ask the namer LLM for up to `count` candidate place names.

        `working` is required — the prompt is built against its city /
        radius / mode. The caller (the `suggest_places` tool) must
        already have refused to call this method when working_location
        was absent (see the tool's no_location guard).

        Returns an empty `CandidateNames(candidates=[])` on any LLM /
        validation failure — the tool maps that to `empty_reason="no_match"`.
        Failures are logged + traced; never raised.
        """
        prompt_template = get_prompt("candidate_namer")
        prompt_text = prompt_template.format(
            intent=intent.strip(),
            location_block=_render_location_block(working),
            mobility_block=_render_mobility_block(working),
            categories_block=_render_list_block(
                [c.value for c in categories] if categories else None,
                "categories",
            ),
            hard_constraints_block=_render_list_block(tags, "hard constraints"),
            taste_block=_render_taste_block(taste_summary),
            count=count,
        )

        span = self._tracer.generation(
            name="candidate_namer",
            input={
                "intent": intent,
                "city": working.city,
                "country": working.country,
                "effective_mode": working.effective_mode,
                "search_radius_m": int(working.search_radius_m),
                "count": count,
            },
            user_id=user_id,
        )
        try:
            response = await self._client.extract(
                response_model=CandidateNames,
                messages=[{"role": "user", "content": prompt_text}],
            )
            result = cast(CandidateNames, response)
            span.end(output={"count": len(result.candidates)})
            return result
        except Exception as exc:
            logger.warning("candidate naming failed: %s", exc, exc_info=True)
            span.end(output={"error": str(exc)}, level="ERROR")
            return CandidateNames(candidates=[])
