"""Claims-driven place retrieval — knowledge that *surfaces* a venue (ADR-138).

Every other read in this layer starts from a place and asks what kebi knows
about it. This one runs the other way: start from what kebi knows about this
area, and let the claims name the places worth putting in the answer.

That inversion is what a local actually does. "Where should I go tonight" in
Canggu on a Monday is not answered by ranking nearby nightclubs — it is
answered by knowing that Monday is Luigi's night. The fact is the retrieval
key; the venue follows from it. Without this path a Monday-night claim could
only ever annotate Luigi's *if some other tool had already found it*, which
is exactly backwards.

No LLM call and no paid provider: one geofenced join over the claims store,
then one batch read of the named places straight from the catalog by id.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from kebi.core.knowledge.candidate_notes_service import _rank
from kebi.core.knowledge.research_models import ResearchNote
from kebi.core.places.models import PlaceCore, PlaceQuery
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation
    from kebi.core.knowledge.schemas import KnowledgeClaim
    from kebi.core.places.protocols import PlacesRepoProtocol

logger = logging.getLogger(__name__)

# Words carrying no topical signal — dropped before overlap scoring so
# "where should I go tonight" doesn't match every claim containing "the".
_STOPWORDS = frozenset(
    """a an and are as at be but by for from how i in is it its me my of on or
    should so that the their there they this to was what when where which who
    will with you your""".split()
)


class KnownPlace:
    """A place the claims store had something to say about, with the notes."""

    __slots__ = ("place", "notes", "score")

    def __init__(self, place: PlaceCore, notes: list[ResearchNote], score: float):
        self.place = place
        self.notes = notes
        self.score = score


# Day-of-week matching is deliberately separate from word overlap. A claim
# saying a venue's big night is "Mondays" must match a question about
# "Monday", and — more importantly — a claim naming ONLY other days is
# positive evidence the place is WRONG for tonight, which plain overlap
# scoring cannot express. Abbreviations included because claims quote signage
# ("Wed-Sat only").
_DAYS: dict[str, str] = {}
for _full, _abbr in (
    ("monday", "mon"),
    ("tuesday", "tue"),
    ("wednesday", "wed"),
    ("thursday", "thu"),
    ("friday", "fri"),
    ("saturday", "sat"),
    ("sunday", "sun"),
):
    for _form in (_full, f"{_full}s", _abbr, f"{_abbr}s", f"{_abbr}."):
        _DAYS[_form] = _full


def _terms(text: str) -> set[str]:
    """Content words, plus a de-pluralised variant of each.

    The singular fold is what makes "Mondays" match "monday" and "drinks"
    match "drink" — cheap, and the alternative (a stemmer dependency) buys
    little on claim-length text.
    """
    words = {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS}
    singular = {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}
    return words | singular


_WEEK_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# "Wed-Sat", "Thu – Sun", "friday to sunday" — a range names every day
# between its ends, and reading it as just the two endpoints makes Friday look
# like a closed day at a Wednesday-to-Saturday venue.
_RANGE_RE = re.compile(
    r"\b([a-z]+\.?)\s*(?:-|–|—|to|through|thru|till|until)\s*([a-z]+\.?)\b"
)


def _expand_range(start: str, end: str) -> set[str]:
    """Every weekday from `start` to `end` inclusive, wrapping over Sunday."""
    i, j = _WEEK_ORDER.index(start), _WEEK_ORDER.index(end)
    span = (j - i) % 7
    return {_WEEK_ORDER[(i + step) % 7] for step in range(span + 1)}


def _days_named(text: str, tags: list[str] | None = None) -> set[str]:
    """Canonical weekday names a claim (or question) refers to.

    Each token is checked both as written and with a trailing period removed,
    because a period is ambiguous here: it ends the abbreviation in "Wed." and
    ends the sentence in "…on Mondays." Testing both forms means neither
    reading is lost.
    """
    lowered = text.lower()
    found: set[str] = set()
    for raw_start, raw_end in _RANGE_RE.findall(lowered):
        start = _DAYS.get(raw_start) or _DAYS.get(raw_start.rstrip("."))
        end = _DAYS.get(raw_end) or _DAYS.get(raw_end.rstrip("."))
        if start and end:
            found |= _expand_range(start, end)
    for raw in re.findall(r"[a-z]+\.?", lowered):
        for form in (raw, raw.rstrip(".")):
            if form in _DAYS:
                found.add(_DAYS[form])
    for tag in tags or []:
        key = tag.strip().lower()
        if key in _DAYS:
            found.add(_DAYS[key])
    return found


# Vocabulary values that describe *when* a place is good. A claim carrying
# one is making a claim about fit with the moment, so it can agree or disagree
# with the turn — unlike a cuisine or atmosphere value, which only ever agrees.
_NEUTRAL_TIME = "all_day"
_NEUTRAL_SEASON = "all_season"
_TIME_VALUES = frozenset(
    {
        "morning",
        "brunch",
        "lunch",
        "afternoon",
        "evening",
        "night",
        "late_night",
        _NEUTRAL_TIME,
    }
)
_SEASON_VALUES = frozenset(
    {"summer", "winter", "rainy", "spring", "autumn", _NEUTRAL_SEASON}
)

# Weight order encodes what actually decides a pick. Day of week is the
# sharpest signal kebi holds ("monday is their night"), so it stays dominant.
# Daypart is next: recommending a breakfast spot at 11pm is wrong however well
# the words match. Taste and season shade the order rather than setting it —
# a place this user would love is worth surfacing above one they would not,
# but never above one that is actually right for tonight.
_DAYPART_MATCH_WEIGHT = 2.5
_DAYPART_MISMATCH_WEIGHT = -1.5
_TASTE_MATCH_WEIGHT = 1.5
_SEASON_MATCH_WEIGHT = 1.0
_SEASON_MISMATCH_WEIGHT = -0.75


def _vocabulary_fit(
    values: set[str],
    vocabulary: frozenset[str],
    target: str | None,
    neutral: str,
    match: float,
    mismatch: float,
) -> float:
    """Score a claim's when-values against the turn's own.

    Silence is not disagreement: a claim carrying no time value is simply not
    making a time argument, so it scores zero rather than being penalised. The
    neutral value ("all_day", "all_season") is an explicit "any", which is a
    mild positive — it confirms fit without claiming to be the best moment.
    """
    if target is None:
        return 0.0
    stated = values & vocabulary
    if not stated:
        return 0.0
    if target in stated:
        return match
    if neutral in stated:
        return match / 2
    return mismatch


# A claim naming today outranks everything else: "Monday is their big night"
# IS the answer to "where tonight", however few words it shares with the
# question. Weighted above tag hits for that reason.
_DAY_MATCH_WEIGHT = 6.0
# A claim naming only other days is evidence against the place for today, but
# it is still worth surfacing — the honest answer names it as a skip ("Vault
# is Wed-Sat, so not tonight") rather than silently dropping it.
_DAY_MISMATCH_WEIGHT = -3.0


def _relevance(
    claim: KnowledgeClaim,
    query_terms: set[str],
    tags: set[str],
    day: str | None = None,
    *,
    daypart: str | None = None,
    season: str | None = None,
    taste: frozenset[str] = frozenset(),
) -> float:
    """How much this claim answers the question being asked.

    Day agreement dominates, then controlled-vocabulary tag hits (a deliberate
    match), then word overlap (which can be coincidence). Confidence is folded
    in last so a strong-but-off-topic claim never outranks a weaker one that
    actually addresses the question.
    """
    claim_tags = {t.strip().lower() for t in claim.tags}
    tag_hits = len(tags & claim_tags)
    word_hits = len(query_terms & _terms(claim.claim))
    score = tag_hits * 2.0 + word_hits + claim.confidence

    score += _vocabulary_fit(
        claim_tags,
        _TIME_VALUES,
        daypart,
        _NEUTRAL_TIME,
        _DAYPART_MATCH_WEIGHT,
        _DAYPART_MISMATCH_WEIGHT,
    )
    score += _vocabulary_fit(
        claim_tags,
        _SEASON_VALUES,
        season,
        _NEUTRAL_SEASON,
        _SEASON_MATCH_WEIGHT,
        _SEASON_MISMATCH_WEIGHT,
    )
    if taste and (claim_tags & taste):
        score += _TASTE_MATCH_WEIGHT

    if day is not None:
        # `_days_named` canonicalises to lowercase; callers pass display-cased
        # weekdays ("Monday"), so fold before comparing — a mismatch here
        # silently applies the wrong-day penalty to a right-day claim.
        target = day.strip().lower()
        claim_days = _days_named(claim.claim, claim.tags)
        if claim_days:
            score += _DAY_MATCH_WEIGHT if target in claim_days else _DAY_MISMATCH_WEIGHT
    return score


class KnownPlacesService:
    """Find the places around here that kebi knows something relevant about."""

    def __init__(
        self,
        repo: KnowledgeClaimRepository,
        places_repo: PlacesRepoProtocol,
        *,
        notes_per_place: int,
        scan_limit: int,
    ) -> None:
        self._repo = repo
        self._places = places_repo
        self._notes_per_place = notes_per_place
        self._scan_limit = scan_limit

    async def find(
        self,
        *,
        working: WorkingLocation,
        query: str,
        tags: list[str] | None,
        user_id: str,
        limit: int,
        day: str | None = None,
        daypart: str | None = None,
        season: str | None = None,
        taste_values: list[str] | None = None,
    ) -> list[KnownPlace]:
        """Places in the turn's geofence ranked by how well their claims fit.

        A place surfaces only if at least one of its claims carries real
        topical signal — otherwise "where should I go tonight" would return
        every venue kebi happens to hold a claim about, which is a catalog
        dump wearing a knowledge badge. When the question carries no signal at
        all (no usable terms, no tags), confidence alone orders the list.

        `day` is the turn's weekday ("Monday"), when the client supplied a
        clock. It makes a schedule claim retrievable rather than incidental:
        with it, "Monday is Luigi's big night" is the top hit for "where
        tonight" even though the two share almost no words. A day named
        explicitly in the question wins over the calendar — "what about
        friday" is about Friday whatever today is.

        `daypart`, `season`, and `taste_values` are the rest of the turn's
        context (ADR-142). They exist so a claim is picked because it fits this
        user at this moment rather than because it shares words with the
        question — a sunset spot at 17:00, a rainy-day room when it is raining,
        a beach club for someone whose saves are all beach clubs.
        """
        if working.search_radius_m <= 0:
            return []
        pairs = await self._repo.list_place_claims_in_area(
            lat=working.lat,
            lng=working.lng,
            radius_m=working.search_radius_m,
            user_id=user_id,
            approved_only=True,
            limit=self._scan_limit,
        )
        if not pairs:
            return []

        query_terms = _terms(query)
        tag_set = {t.strip().lower() for t in (tags or [])}
        taste = frozenset(v.strip().lower() for v in (taste_values or []) if v.strip())
        asked_days = _days_named(query, tags)
        effective_day = next(iter(sorted(asked_days))) if asked_days else day
        has_signal = bool(query_terms or tag_set or effective_day)

        by_place: dict[str, list[KnowledgeClaim]] = defaultdict(list)
        best: dict[str, float] = {}
        for place_id, claim in pairs:
            score = _relevance(
                claim,
                query_terms,
                tag_set,
                effective_day,
                daypart=daypart,
                season=season,
                taste=taste,
            )
            # A claim that names ANY day answers a day-scoped question, even
            # when the day is the wrong one — "Vault is Wed-Sat" is exactly
            # what the answer needs to say "not tonight". Its negative score
            # keeps it below the right-day picks without hiding it.
            speaks_to_the_day = effective_day is not None and bool(
                _days_named(claim.claim, claim.tags)
            )
            # With topical signal, a claim earns its place only by matching
            # something asked for; `_relevance` floors at `confidence`, so
            # anything at or below that matched nothing.
            if has_signal and not speaks_to_the_day and score <= claim.confidence:
                continue
            by_place[place_id].append(claim)
            best[place_id] = max(best.get(place_id, 0.0), score)
        if not by_place:
            return []

        ranked_ids = sorted(best, key=lambda pid: best[pid], reverse=True)[:limit]
        # Catalog-only read by known id — no provider call, no cost.
        places = await self._places.find(
            PlaceQuery(ids=ranked_ids), limit=len(ranked_ids)
        )
        by_id = {p.id: p for p in places if p.id}

        results: list[KnownPlace] = []
        for place_id in ranked_ids:
            place = by_id.get(place_id)
            if place is None:
                # Claim outlived its place row (catalog wipe, ADR-118 TTL).
                continue
            results.append(
                KnownPlace(
                    place=place,
                    notes=_rank(by_place[place_id], self._notes_per_place),
                    score=best[place_id],
                )
            )
        return results
