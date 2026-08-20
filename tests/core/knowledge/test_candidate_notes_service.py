"""Tests for `CandidateNotesService` — insider claims on the retrieval path
(ADR-137)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kebi.core.agent.location import WorkingLocation
from kebi.core.knowledge.candidate_notes_service import CandidateNotesService
from kebi.core.knowledge.schemas import KnowledgeClaim
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_NOW = datetime(2026, 8, 6, tzinfo=UTC)

_BADUNG = make_city("id", "Badung")
_CANGGU = make_area(_BADUNG, "Canggu")


def _claim(
    entity_key: str,
    text: str,
    *,
    confidence: float = 0.5,
    agree: int = 0,
    disagree: int = 0,
    age_days: int = 0,
    source_type: str = "shared_content",
) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=f"c-{entity_key}-{text}",
        entity_type="place" if entity_key.startswith("place:") else "city",
        entity_key=entity_key,
        entity_name="whatever",
        claim=text,
        source_type=source_type,  # type: ignore[arg-type]
        confidence=confidence,
        agree_count=agree,
        disagree_count=disagree,
        created_at=_NOW - timedelta(days=age_days),
    )


class _FakeRepo:
    """Records the keys asked for and replays a canned claim list."""

    def __init__(self, claims: list[KnowledgeClaim]) -> None:
        self._claims = claims
        self.requested_keys: list[str] = []
        self.approved_only: bool | None = None
        self.user_id: str | None = None

    async def list_for_entities(
        self, entity_keys: list[str], *, user_id: str, approved_only: bool
    ) -> list[KnowledgeClaim]:
        self.requested_keys = list(entity_keys)
        self.user_id = user_id
        self.approved_only = approved_only
        return [c for c in self._claims if c.entity_key in set(entity_keys)]


def _service(
    claims: list[KnowledgeClaim], **kw: Any
) -> tuple[CandidateNotesService, _FakeRepo]:
    repo = _FakeRepo(claims)
    service = CandidateNotesService(
        repo,  # type: ignore[arg-type]
        FakeGeoRegistry(_BADUNG, _CANGGU),
        per_place_limit=kw.get("per_place_limit", 2),
        area_limit=kw.get("area_limit", 3),
    )
    return service, repo


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Indonesia",
        "country_code": "id",
        "city": "Badung",
        "neighborhood": "Canggu",
        "lat": -8.65,
        "lng": 115.13,
    }
    base.update(kw)
    return WorkingLocation(**base)


class TestNotesForPlaces:
    async def test_groups_claims_under_their_place_id(self) -> None:
        service, repo = _service(
            [
                _claim("place:p1", "monday is the big night"),
                _claim("place:p2", "cash only"),
            ]
        )
        result = await service.notes_for_places(["p1", "p2"], "user-1")
        assert [n.text for n in result["p1"]] == ["monday is the big night"]
        assert [n.text for n in result["p2"]] == ["cash only"]
        assert set(repo.requested_keys) == {"place:p1", "place:p2"}

    async def test_places_without_claims_are_absent_not_empty(self) -> None:
        service, _ = _service([_claim("place:p1", "a note")])
        result = await service.notes_for_places(["p1", "p2"], "user-1")
        assert "p2" not in result

    async def test_reads_are_approved_only_and_user_scoped(self) -> None:
        service, repo = _service([])
        await service.notes_for_places(["p1"], "user-7")
        assert repo.approved_only is True
        assert repo.user_id == "user-7"

    async def test_no_place_ids_makes_no_read_at_all(self) -> None:
        service, repo = _service([_claim("place:p1", "a note")])
        assert await service.notes_for_places([], "user-1") == {}
        assert repo.requested_keys == []

    async def test_corroboration_outranks_confidence(self) -> None:
        service, _ = _service(
            [
                _claim("place:p1", "lone guess", confidence=0.99),
                _claim("place:p1", "two people agree", confidence=0.4, agree=2),
            ]
        )
        result = await service.notes_for_places(["p1"], "user-1")
        assert [n.text for n in result["p1"]] == ["two people agree", "lone guess"]

    async def test_per_place_limit_caps_the_list(self) -> None:
        service, _ = _service(
            [_claim("place:p1", f"note {i}", confidence=i / 10) for i in range(5)],
            per_place_limit=2,
        )
        result = await service.notes_for_places(["p1"], "user-1")
        assert len(result["p1"]) == 2

    async def test_source_is_the_coarse_label_not_the_raw_type(self) -> None:
        service, _ = _service(
            [_claim("place:p1", "note", source_type="curated_expert")]
        )
        result = await service.notes_for_places(["p1"], "user-1")
        assert result["p1"][0].source == "expert"


class TestNotesForArea:
    async def test_reads_country_city_and_neighborhood_keys(self) -> None:
        service, repo = _service([])
        await service.notes_for_area(_working(), "user-1")
        assert repo.requested_keys == ["id", _BADUNG.geo_key, _CANGGU.geo_key]

    async def test_unregistered_city_contributes_only_the_country_key(self) -> None:
        """The read path never mints: an area the registry hasn't met simply
        contributes no notes yet, and the country key still reads."""
        service, repo = _service([])
        await service.notes_for_area(_working(city="Ghost Town"), "user-1")
        assert repo.requested_keys == ["id"]

    async def test_all_levels_rank_as_one_pool(self) -> None:
        service, _ = _service(
            [
                _claim("id", "cash still rules outside the cities", confidence=0.9),
                _claim(_CANGGU.geo_key, "packed after 11", confidence=0.5),
            ]
        )
        notes = await service.notes_for_area(_working(), "user-1")
        assert [n.text for n in notes] == [
            "cash still rules outside the cities",
            "packed after 11",
        ]

    async def test_area_limit_caps_the_pool(self) -> None:
        service, _ = _service(
            [_claim("id", f"note {i}", confidence=i / 10) for i in range(6)],
            area_limit=3,
        )
        assert len(await service.notes_for_area(_working(), "user-1")) == 3

    async def test_no_location_means_no_read(self) -> None:
        service, repo = _service([_claim("id", "a note")])
        assert await service.notes_for_area(None, "user-1") == []
        assert repo.requested_keys == []

    async def test_missing_country_code_means_no_canonical_key(self) -> None:
        service, repo = _service([_claim("id", "a note")])
        assert await service.notes_for_area(_working(country_code=None), "u") == []
        assert repo.requested_keys == []
