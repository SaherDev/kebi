"""Tests for `attach_notes` — the shared applier that puts kebi's insider
claims on a place tool's result (ADR-137)."""

from __future__ import annotations

from typing import Any

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools._notes import attach_notes
from kebi.core.agent.tools.consult_models import ConsultCandidate, ConsultResult
from kebi.core.knowledge.research_models import ResearchNote
from kebi.core.places.models import PlaceCore


def _note(text: str) -> ResearchNote:
    return ResearchNote(id=f"n-{text}", text=text, source="community", confidence=0.5)


def _result(*place_ids: str) -> ConsultResult:
    return ConsultResult(
        candidates=[
            ConsultCandidate(
                place=PlaceCore(id=pid, place_name=f"Place {pid}"),
                source="saved",
                rrf_score=0.5,
            )
            for pid in place_ids
        ]
    )


class _FakeNotes:
    def __init__(
        self,
        by_place: dict[str, list[ResearchNote]] | None = None,
        area: list[ResearchNote] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self._by_place = by_place or {}
        self._area = area or []
        self._raises = raises
        self.seen_place_ids: list[str] | None = None

    async def notes_for_places(
        self, place_ids: list[str], user_id: str
    ) -> dict[str, list[ResearchNote]]:
        if self._raises:
            raise RuntimeError("claims read blew up")
        self.seen_place_ids = place_ids
        return self._by_place

    async def notes_for_area(
        self, working: WorkingLocation | None, user_id: str
    ) -> list[ResearchNote]:
        return self._area


async def _attach(result: ConsultResult, service: Any) -> ConsultResult:
    return await attach_notes(
        result, notes_service=service, user_id="user-1", working=None
    )


async def test_per_candidate_notes_land_on_their_own_candidate() -> None:
    service = _FakeNotes({"p1": [_note("monday is the big night")]})
    out = await _attach(_result("p1", "p2"), service)
    assert [n.text for n in out.candidates[0].notes] == ["monday is the big night"]
    assert out.candidates[1].notes == []


async def test_area_notes_land_on_the_result() -> None:
    out = await _attach(_result("p1"), _FakeNotes(area=[_note("packed after 11")]))
    assert [n.text for n in out.area_notes] == ["packed after 11"]


async def test_area_notes_ride_an_empty_candidate_list() -> None:
    # An empty search with real area knowledge still has something to say.
    out = await _attach(
        ConsultResult(empty_reason="no_match"), _FakeNotes(area=[_note("a")])
    )
    assert len(out.area_notes) == 1
    assert out.empty_reason == "no_match"


async def test_candidates_without_a_catalog_id_are_not_looked_up() -> None:
    service = _FakeNotes()
    result = ConsultResult(
        candidates=[
            ConsultCandidate(
                place=PlaceCore(id=None, place_name="Unsaved"),
                source="discovered",
                rrf_score=0.0,
            )
        ]
    )
    await _attach(result, service)
    assert service.seen_place_ids == []


async def test_no_service_is_an_identity_function() -> None:
    result = _result("p1")
    assert (
        await attach_notes(result, notes_service=None, user_id="user-1", working=None)
        is result
    )


async def test_a_failed_claims_read_never_fails_the_recommendation() -> None:
    result = _result("p1")
    out = await _attach(result, _FakeNotes(raises=True))
    assert out is result
    assert out.candidates[0].notes == []
