"""Attach kebi's insider claims to a place tool's result (ADR-137).

One helper, called by all three place tools just before they pack their
`ConsultResult`, so what kebi knows rides every recommendation turn instead of
waiting for a `research` call that a 5-call budget rarely affords.

Best-effort by construction: a claims read that fails must never fail the
recommendation. The tool's real work — finding places — has already happened
by the time this runs, so an exception here is logged and swallowed and the
result goes out with no notes.
"""

from __future__ import annotations

import logging

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools.consult_models import ConsultResult
from kebi.core.knowledge.candidate_notes_service import CandidateNotesService

logger = logging.getLogger(__name__)


async def attach_notes(
    result: ConsultResult,
    *,
    notes_service: CandidateNotesService | None,
    user_id: str,
    working: WorkingLocation | None,
) -> ConsultResult:
    """Return `result` with per-candidate `notes` and result-level `area_notes`.

    `notes_service` is `None` when the knowledge layer isn't wired (tests,
    minimal graphs) — then this is an identity function.
    """
    if notes_service is None:
        return result
    try:
        place_ids = [c.place.id for c in result.candidates if c.place.id is not None]
        by_place = await notes_service.notes_for_places(place_ids, user_id)
        area_notes = await notes_service.notes_for_area(working, user_id)
    except Exception:
        logger.exception("attach_notes failed; returning result without notes")
        return result

    if not by_place and not area_notes:
        return result

    candidates = [
        c.model_copy(update={"notes": by_place.get(c.place.id or "", [])})
        for c in result.candidates
    ]
    return result.model_copy(
        update={"candidates": candidates, "area_notes": area_notes}
    )
