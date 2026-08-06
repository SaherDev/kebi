"""Tests for `KnownPlacesService` — claims-driven place retrieval (ADR-138).

The behaviour under test is the inversion: a fact selects the place. The
motivating case is the whole suite in miniature — "where should I go tonight"
on a Monday must return Luigi's because a claim says Mondays, not because
Luigi's ranks well as a nightclub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kebi.core.agent.location import WorkingLocation
from kebi.core.knowledge.known_places_service import KnownPlacesService
from kebi.core.knowledge.schemas import KnowledgeClaim
from kebi.core.places.models import PlaceCore

_NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _claim(
    place_id: str, text: str, *, confidence: float = 0.8, tags: list[str] | None = None
) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=f"c-{place_id}-{text[:12]}",
        entity_type="place",
        entity_key=f"place:{place_id}",
        entity_name=place_id,
        claim=text,
        tags=tags or [],
        source_type="shared_content",
        confidence=confidence,
        created_at=_NOW,
    )


class _FakeClaimRepo:
    def __init__(self, pairs: list[tuple[str, KnowledgeClaim]]) -> None:
        self._pairs = pairs
        self.called_with: dict[str, Any] | None = None

    async def list_place_claims_in_area(self, **kw: Any) -> list[tuple[str, Any]]:
        self.called_with = kw
        return self._pairs


class _FakePlacesRepo:
    def __init__(self, places: dict[str, str]) -> None:
        self._places = places
        self.requested_ids: list[str] | None = None

    async def find(self, query: Any, limit: int = 20) -> list[PlaceCore]:
        self.requested_ids = list(query.ids or [])
        return [
            PlaceCore(id=pid, place_name=self._places[pid])
            for pid in self.requested_ids
            if pid in self._places
        ]


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Indonesia",
        "country_code": "id",
        "city": "Bali",
        "neighborhood": "Canggu",
        "lat": -8.65,
        "lng": 115.13,
        "search_radius_m": 3000.0,
    }
    base.update(kw)
    return WorkingLocation(**base)


def _service(
    pairs: list[tuple[str, KnowledgeClaim]],
    places: dict[str, str],
    **kw: Any,
) -> tuple[KnownPlacesService, _FakeClaimRepo, _FakePlacesRepo]:
    claim_repo = _FakeClaimRepo(pairs)
    places_repo = _FakePlacesRepo(places)
    service = KnownPlacesService(
        claim_repo,  # type: ignore[arg-type]
        places_repo,  # type: ignore[arg-type]
        notes_per_place=kw.get("notes_per_place", 3),
        scan_limit=kw.get("scan_limit", 300),
    )
    return service, claim_repo, places_repo


_LUIGIS = ("p-luigis", "Luigi's Hot Pizza Canggu")
_VAULT = ("p-vault", "Vault Nightclub Bali")


def _nightlife_pairs() -> list[tuple[str, KnowledgeClaim]]:
    return [
        (
            _LUIGIS[0],
            _claim(
                _LUIGIS[0],
                "Luigi's Hot Pizza is famous for its house and techno music "
                "nights on Mondays in Canggu.",
                confidence=0.9,
            ),
        ),
        (
            _VAULT[0],
            _claim(
                _VAULT[0],
                "Vault Nightclub runs Wed-Sat only.",
                confidence=0.95,
            ),
        ),
    ]


_NIGHTLIFE_PLACES = {_LUIGIS[0]: _LUIGIS[1], _VAULT[0]: _VAULT[1]}


class TestDayOfWeek:
    async def test_todays_claim_leads_even_with_no_shared_words(self) -> None:
        """The motivating case: Monday picks Luigi's.

        "night out" shares nothing with the Luigi's claim except the day, and
        the Vault claim is more confident — so only day matching gets this
        right.
        """
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="night out",
            tags=None,
            user_id="u-1",
            limit=5,
            day="Monday",
        )
        assert found[0].place.place_name == _LUIGIS[1]

    async def test_plural_weekday_in_a_claim_still_matches(self) -> None:
        # "Mondays" in the claim vs "Monday" as the day — a plain word-overlap
        # match misses this, which is exactly how Luigi's was missed live.
        service, _, _ = _service(
            [(_LUIGIS[0], _claim(_LUIGIS[0], "Big night is Mondays."))],
            {_LUIGIS[0]: _LUIGIS[1]},
        )
        found = await service.find(
            working=_working(),
            query="tonight",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        assert [f.place.place_name for f in found] == [_LUIGIS[1]]

    async def test_a_wrong_day_place_is_surfaced_but_ranked_below(self) -> None:
        # Kept so the answer can say "that one's Wed-Sat, not tonight" —
        # dropping it silently would lose a useful line.
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="night out",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        names = [f.place.place_name for f in found]
        assert names.index(_LUIGIS[1]) < names.index(_VAULT[1])

    async def test_a_day_named_in_the_question_beats_the_calendar(self) -> None:
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="what about friday",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        assert found[0].place.place_name == _VAULT[1]

    async def test_no_clock_falls_back_to_topical_matching(self) -> None:
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="techno music",
            tags=None,
            user_id="u",
            limit=5,
            day=None,
        )
        assert found[0].place.place_name == _LUIGIS[1]


class TestRelevance:
    async def test_a_place_with_nothing_relevant_does_not_surface(self) -> None:
        service, _, _ = _service(
            [("p-x", _claim("p-x", "The garden is pretty in spring."))],
            {"p-x": "Somewhere"},
        )
        found = await service.find(
            working=_working(),
            query="ATM without fees",
            tags=None,
            user_id="u",
            limit=5,
        )
        assert found == []

    async def test_tag_hits_outweigh_word_overlap(self) -> None:
        service, _, _ = _service(
            [
                ("p-tag", _claim("p-tag", "No surcharge here.", tags=["no_fee_atm"])),
                ("p-word", _claim("p-word", "The ATM is by the door.")),
            ],
            {"p-tag": "Tagged", "p-word": "Worded"},
        )
        found = await service.find(
            working=_working(), query="ATM", tags=["no_fee_atm"], user_id="u", limit=5
        )
        assert found[0].place.place_name == "Tagged"

    async def test_notes_carry_the_facts_that_surfaced_the_place(self) -> None:
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="night out",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        assert "Mondays" in found[0].notes[0].text

    async def test_notes_per_place_is_capped(self) -> None:
        pairs = [
            (
                _LUIGIS[0],
                _claim(_LUIGIS[0], f"Monday fact {i}", confidence=0.5 + i / 20),
            )
            for i in range(5)
        ]
        service, _, _ = _service(pairs, {_LUIGIS[0]: _LUIGIS[1]}, notes_per_place=2)
        found = await service.find(
            working=_working(),
            query="tonight",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        assert len(found[0].notes) == 2


class TestReadBehaviour:
    async def test_geofence_comes_from_the_turns_working_location(self) -> None:
        service, claim_repo, _ = _service([], {})
        await service.find(
            working=_working(search_radius_m=1234.0),
            query="anything",
            tags=None,
            user_id="u-9",
            limit=5,
        )
        assert claim_repo.called_with is not None
        assert claim_repo.called_with["radius_m"] == 1234.0
        assert claim_repo.called_with["user_id"] == "u-9"
        assert claim_repo.called_with["approved_only"] is True

    async def test_no_radius_means_no_read(self) -> None:
        service, claim_repo, _ = _service([], {})
        assert (
            await service.find(
                working=_working(search_radius_m=0.0),
                query="anything",
                tags=None,
                user_id="u",
                limit=5,
            )
            == []
        )
        assert claim_repo.called_with is None

    async def test_limit_caps_the_places_looked_up(self) -> None:
        pairs = [
            (f"p{i}", _claim(f"p{i}", "monday night", confidence=0.5 + i / 100))
            for i in range(8)
        ]
        service, _, places_repo = _service(pairs, {f"p{i}": f"P{i}" for i in range(8)})
        await service.find(
            working=_working(), query="monday night", tags=None, user_id="u", limit=3
        )
        assert places_repo.requested_ids is not None
        assert len(places_repo.requested_ids) == 3

    async def test_a_claim_whose_place_row_is_gone_is_skipped(self) -> None:
        # Catalog TTL wipe (ADR-118) can outlive a claim.
        service, _, _ = _service(
            [("p-gone", _claim("p-gone", "monday night"))],
            {},
        )
        found = await service.find(
            working=_working(), query="monday night", tags=None, user_id="u", limit=5
        )
        assert found == []


class TestDayRanges:
    """ "Wed-Sat" names four days, not two — reading only the endpoints makes
    Friday look like a closed day at a Wednesday-to-Saturday venue."""

    async def test_a_day_inside_a_range_counts_as_open(self) -> None:
        service, _, _ = _service(
            [(_VAULT[0], _claim(_VAULT[0], "Vault runs Wed-Sat only."))],
            {_VAULT[0]: _VAULT[1]},
        )
        found = await service.find(
            working=_working(),
            query="tonight",
            tags=None,
            user_id="u",
            limit=5,
            day="Friday",
        )
        assert [f.place.place_name for f in found] == [_VAULT[1]]

    async def test_a_day_outside_a_range_still_ranks_below(self) -> None:
        service, _, _ = _service(_nightlife_pairs(), _NIGHTLIFE_PLACES)
        found = await service.find(
            working=_working(),
            query="tonight",
            tags=None,
            user_id="u",
            limit=5,
            day="Monday",
        )
        names = [f.place.place_name for f in found]
        assert names.index(_LUIGIS[1]) < names.index(_VAULT[1])


class TestTurnContextRanking:
    """A claim is picked because it fits this user at this moment, not because
    it shares words with the question (ADR-142)."""

    @staticmethod
    def _pair(pid: str, text: str, tags: list[str], conf: float = 0.8):
        return (pid, _claim(pid, text, confidence=conf, tags=tags))

    async def test_daypart_decides_between_equally_wordy_claims(self) -> None:
        service, _, _ = _service(
            [
                self._pair("p-am", "great spot", ["morning"]),
                self._pair("p-pm", "great spot", ["evening"]),
            ],
            {"p-am": "Breakfast Place", "p-pm": "Dinner Place"},
        )
        found = await service.find(
            working=_working(),
            query="great spot",
            tags=None,
            user_id="u",
            limit=5,
            daypart="evening",
        )
        assert found[0].place.place_name == "Dinner Place"

    async def test_a_wrong_daypart_claim_ranks_below(self) -> None:
        service, _, _ = _service(
            [
                self._pair("p-am", "great spot", ["morning"], conf=0.95),
                self._pair("p-pm", "great spot", ["evening"], conf=0.5),
            ],
            {"p-am": "Breakfast Place", "p-pm": "Dinner Place"},
        )
        names = [
            f.place.place_name
            for f in await service.find(
                working=_working(),
                query="great spot",
                tags=None,
                user_id="u",
                limit=5,
                daypart="evening",
            )
        ]
        assert names.index("Dinner Place") < names.index("Breakfast Place")

    async def test_all_day_is_a_mild_yes_not_a_mismatch(self) -> None:
        service, _, _ = _service(
            [
                self._pair("p-any", "great spot", ["all_day"]),
                self._pair("p-am", "great spot", ["morning"]),
            ],
            {"p-any": "Anytime Place", "p-am": "Breakfast Place"},
        )
        names = [
            f.place.place_name
            for f in await service.find(
                working=_working(),
                query="great spot",
                tags=None,
                user_id="u",
                limit=5,
                daypart="evening",
            )
        ]
        assert names.index("Anytime Place") < names.index("Breakfast Place")

    async def test_a_claim_making_no_time_argument_is_not_penalised(self) -> None:
        # Silence is not disagreement — most claims carry no time value at all.
        service, _, _ = _service(
            [self._pair("p-x", "genuinely great", [])], {"p-x": "Quiet Place"}
        )
        found = await service.find(
            working=_working(),
            query="genuinely great",
            tags=None,
            user_id="u",
            limit=5,
            daypart="evening",
        )
        assert [f.place.place_name for f in found] == ["Quiet Place"]

    async def test_taste_lifts_a_place_the_user_would_like(self) -> None:
        service, _, _ = _service(
            [
                self._pair("p-beach", "worth a visit", ["beach"]),
                self._pair("p-other", "worth a visit", ["museum"]),
            ],
            {"p-beach": "Beach Club", "p-other": "Museum"},
        )
        found = await service.find(
            working=_working(),
            query="worth a visit",
            tags=None,
            user_id="u",
            limit=5,
            taste_values=["beach"],
        )
        assert found[0].place.place_name == "Beach Club"

    async def test_taste_never_outranks_being_right_for_tonight(self) -> None:
        # Ordering rule: fit-for-the-moment beats fit-for-the-person.
        service, _, _ = _service(
            [
                self._pair("p-beach", "worth a visit", ["beach", "morning"]),
                self._pair("p-bar", "worth a visit", ["evening"]),
            ],
            {"p-beach": "Beach Club", "p-bar": "Night Bar"},
        )
        names = [
            f.place.place_name
            for f in await service.find(
                working=_working(),
                query="worth a visit",
                tags=None,
                user_id="u",
                limit=5,
                daypart="evening",
                taste_values=["beach"],
            )
        ]
        assert names.index("Night Bar") < names.index("Beach Club")

    async def test_season_shades_the_order(self) -> None:
        service, _, _ = _service(
            [
                self._pair("p-sun", "nice place", ["summer"]),
                self._pair("p-cold", "nice place", ["winter"]),
            ],
            {"p-sun": "Terrace", "p-cold": "Fireplace"},
        )
        names = [
            f.place.place_name
            for f in await service.find(
                working=_working(),
                query="nice place",
                tags=None,
                user_id="u",
                limit=5,
                season="summer",
            )
        ]
        assert names.index("Terrace") < names.index("Fireplace")

    async def test_no_context_leaves_ranking_unchanged(self) -> None:
        # Absent signals must not perturb the order (the common case today).
        pairs = [
            self._pair("p-a", "great spot", ["morning"], conf=0.9),
            self._pair("p-b", "great spot", ["evening"], conf=0.5),
        ]
        service, _, _ = _service(pairs, {"p-a": "A", "p-b": "B"})
        found = await service.find(
            working=_working(), query="great spot", tags=None, user_id="u", limit=5
        )
        assert [f.place.place_name for f in found] == ["A", "B"]
