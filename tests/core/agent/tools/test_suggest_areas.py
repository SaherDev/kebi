"""`suggest_areas` — areas the agent named, verified, carded, and anchored.

Two things this tool must get right, and both are tested here: a verified area
becomes an AREA candidate (never a venue, so never a savable venue row), and
the verified entities land on state as anchors so the place tools that run
next search at them rather than around the turn's origin.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langgraph.types import Command

from kebi.core.agent.tools.consult_models import ConsultResult
from kebi.core.agent.tools.suggest_areas_tool import _run_suggest_areas
from kebi.core.areas.models import AreaEntity, AreaSummary
from kebi.core.areas.suggestion_service import AreaSuggestion, AreaSuggestionResult


def _entity(
    key: str = "vn/da-nang/an-thuong",
    name: str = "An Thuong",
    kind: str = "neighborhood",
    bbox: list[float] | None = None,
) -> AreaEntity:
    return AreaEntity(
        entity_key=key,
        entity_type=kind,  # type: ignore[arg-type]
        name=name,
        country_code="vn",
        lat=16.04,
        lng=108.24,
        bbox=bbox,
    )


def _service(result: AreaSuggestionResult) -> MagicMock:
    svc = MagicMock()
    svc.suggest = AsyncMock(return_value=result)
    return svc


def _suggestion(entity: AreaEntity) -> AreaSuggestion:
    return AreaSuggestion(entity=entity, summary=AreaSummary.from_entity(entity))


def _state() -> dict[str, Any]:
    return {
        "messages": [],
        "user_id": "user-1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_location": None,
        "working_location": None,
        "location_clarification": None,
        "movement_profile": None,
        "reasoning_steps": [],
        "area_anchors": [],
        "steps_taken": 0,
        "error_count": 0,
        "tool_calls_used": 0,
    }


async def _run(service: MagicMock, names: list[str], limit: int = 5) -> Command[Any]:
    return await _run_suggest_areas(
        service=service,
        state=_state(),
        tool_call_id="tc-1",
        names=names,
        city=None,
        country="Vietnam",
        limit=limit,
    )


def _payload(cmd: Command[Any]) -> ConsultResult:
    return ConsultResult.model_validate_json(cmd.update["messages"][0].content)


async def test_a_verified_area_comes_back_as_an_area_candidate() -> None:
    cmd = await _run(
        _service(AreaSuggestionResult(suggestions=[_suggestion(_entity())])),
        ["An Thuong"],
    )
    candidate = _payload(cmd).candidates[0]
    assert candidate.kind == "area"
    assert candidate.area is not None and candidate.area.name == "An Thuong"
    # A venue payload would carry a save action for a place that is not one.
    assert candidate.place is None


async def test_verified_areas_become_anchors_on_state() -> None:
    """The half that makes this more than a card: the place tools read these
    to search AT each named area (ADR-140)."""
    entity = _entity()
    cmd = await _run(
        _service(AreaSuggestionResult(suggestions=[_suggestion(entity)])), ["An Thuong"]
    )
    anchors = cmd.update["area_anchors"]
    assert [a["entity_key"] for a in anchors] == ["vn/da-nang/an-thuong"]


async def test_an_unbelievable_extent_is_not_handed_to_the_client() -> None:
    """Google returns a ~0.4 km box for the ~20 km Hai Van Pass. Rendering it
    would shade one hairpin and call it the pass, so the card degrades to a
    point instead."""
    pass_entity = _entity(
        key="vn/da-nang/hai-van-pass",
        name="Hai Van Pass",
        kind="natural_feature",
        bbox=[16.199, 16.201, 108.129, 108.131],
    )
    cmd = await _run(
        _service(AreaSuggestionResult(suggestions=[_suggestion(pass_entity)])),
        ["Hai Van Pass"],
    )
    candidate = _payload(cmd).candidates[0]
    assert candidate.extent is None
    assert candidate.area is not None and candidate.area.extent is None


async def test_a_believable_extent_reaches_the_client() -> None:
    entity = _entity(bbox=[16.03, 16.05, 108.23, 108.25])
    cmd = await _run(
        _service(AreaSuggestionResult(suggestions=[_suggestion(entity)])), ["An Thuong"]
    )
    assert _payload(cmd).candidates[0].extent == [16.03, 16.05, 108.23, 108.25]


async def test_every_name_refused_is_an_honest_empty_not_an_error() -> None:
    cmd = await _run(
        _service(AreaSuggestionResult(refused=["Ha Giang Loop"])), ["Ha Giang Loop"]
    )
    payload = _payload(cmd)
    assert payload.candidates == []
    assert payload.empty_reason == "no_match"
    assert cmd.update["area_anchors"] == []


async def test_refusals_are_narrated_alongside_what_did_verify() -> None:
    """Never a silent drop — the agent has to know a name didn't check out so
    it can say so rather than quietly answering about fewer places."""
    cmd = await _run(
        _service(
            AreaSuggestionResult(
                suggestions=[_suggestion(_entity())], refused=["Ha Giang Loop"]
            )
        ),
        ["An Thuong", "Ha Giang Loop"],
    )
    summary = cmd.update["reasoning_steps"][-1].summary
    assert "1 area" in summary
    assert "didn't check out" in summary


async def test_no_names_spends_nothing() -> None:
    service = _service(AreaSuggestionResult())
    cmd = await _run(service, ["", "  "])
    service.suggest.assert_not_awaited()
    assert _payload(cmd).empty_reason == "no_match"


async def test_a_resolution_failure_degrades_rather_than_killing_the_turn() -> None:
    service = MagicMock()
    service.suggest = AsyncMock(side_effect=RuntimeError("geocoder down"))
    cmd = await _run(service, ["An Thuong"])
    assert _payload(cmd).empty_reason == "error"


async def test_limit_caps_the_cards_and_the_anchors_together() -> None:
    entities = [
        _entity(key=f"vn/c{i}", name=f"Area {i}", kind="city") for i in range(5)
    ]
    cmd = await _run(
        _service(AreaSuggestionResult(suggestions=[_suggestion(e) for e in entities])),
        [e.name for e in entities],
        limit=2,
    )
    assert len(_payload(cmd).candidates) == 2
    assert len(cmd.update["area_anchors"]) == 2
