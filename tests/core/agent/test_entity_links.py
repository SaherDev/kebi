"""Tests for the chat entity-link layer (ADR-136).

Chat's whole render contract is text plus `kebi://` links, so these cover
both halves: which entities a turn can link (`build_entity_index`) and how
the answer text is rewritten (`linkify`).
"""

from __future__ import annotations

from typing import Any

from kebi.core.agent.entity_links import (
    build_entity_index,
    linkify,
    normalize_voice,
    turn_recommendation_id,
    working_location_entity_pairs,
)
from kebi.core.areas.keys import encode_area_id
from kebi.core.web.keys import encode_web_url
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

# Geo keys are registry id-paths now; the rows the working location resolves
# to are seeded here, exactly as the real registry would hold them.
_BADUNG = make_city("id", "Badung")
_CANGGU_ROW = make_area(_BADUNG, "Canggu")
_REGISTRY = FakeGeoRegistry(_BADUNG, _CANGGU_ROW)

# Area URIs carry the geo key encoded as one opaque segment (ADR-153); the
# raw key stays on the entity's `key` field.
_CANGGU_URI = f"kebi://area/{encode_area_id(_CANGGU_ROW.geo_key)}"
_BADUNG_URI = f"kebi://area/{encode_area_id(_BADUNG.geo_key)}"


def _place_result(
    *,
    tool: str = "find_saved",
    candidates: list[dict[str, Any]] | None = None,
    recommendation_id: str = "rec-1",
) -> dict[str, Any]:
    return {
        "tool": tool,
        "tool_call_id": "call-1",
        "payload": {
            "candidates": candidates if candidates is not None else [],
            "recommendation_id": recommendation_id,
        },
    }


def _candidate(
    place_id: str,
    name: str,
    aliases: list[str] | None = None,
    icon: str | None = None,
) -> dict[str, Any]:
    return {
        "place": {
            "id": place_id,
            "place_name": name,
            "place_name_aliases": [
                {"value": a, "source": "tiktok"} for a in (aliases or [])
            ],
            "icon": icon,
        },
        "source": "saved",
    }


_CANGGU = {
    "country": "Indonesia",
    "country_code": "id",
    "city": "Badung",
    "neighborhood": "Canggu",
    "lat": -8.65,
    "lng": 115.13,
}


async def _wl_pairs(working_location: Any) -> list[Any]:
    """Pre-resolved working-location pairs, as the chat routes supply them."""
    return await working_location_entity_pairs(_REGISTRY, working_location)


class TestBuildEntityIndex:
    def test_place_candidates_become_venue_entities(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigi's Hot Pizza")])]
        )
        # The full name plus its spoken short forms, all one entity.
        assert {e.uri for _, e in index} == {"kebi://venue/p1"}
        assert {e.kind for _, e in index} == {"venue"}
        assert dict(index).keys() >= {"Luigi's Hot Pizza", "Luigi's"}

    def test_aliases_map_to_the_same_entity(self) -> None:
        index = build_entity_index(
            [
                _place_result(
                    candidates=[_candidate("p1", "Luigi's Hot Pizza", ["Luigis"])]
                )
            ]
        )
        assert {alias for alias, _ in index} >= {"Luigi's Hot Pizza", "Luigis"}
        assert {e.key for _, e in index} == {"p1"}

    def test_longest_alias_first_so_overlaps_resolve(self) -> None:
        index = build_entity_index(
            [
                _place_result(
                    candidates=[
                        _candidate("p1", "Luigis"),
                        _candidate("p2", "Luigis Hot Pizza"),
                    ]
                )
            ]
        )
        # Longest first is what makes overlaps resolve; "Luigis" is claimed by
        # p1 outright, so it is not offered as a short form of p2.
        aliases = [alias for alias, _ in index]
        assert aliases[0] == "Luigis Hot Pizza"
        assert aliases.index("Luigis Hot Pizza") < aliases.index("Luigis")
        assert dict(index)["Luigis"].key == "p1"

    async def test_working_location_contributes_area_entities(self) -> None:
        index = build_entity_index([], await _wl_pairs(_CANGGU))
        by_alias = dict(index)
        assert by_alias["Canggu"].uri == _CANGGU_URI
        assert by_alias["Badung"].uri == _BADUNG_URI

    async def test_working_location_without_country_code_is_not_linkable(self) -> None:
        # No ISO code, no canonical key — an unkeyed area cannot be tapped.
        assert await _wl_pairs({**_CANGGU, "country_code": None}) == []

    async def test_working_location_sentinel_string_is_ignored(self) -> None:
        # First turn can leave the carry-forward sentinel in the state slot.
        assert await _wl_pairs("__inherit__") == []

    def test_research_geo_key_becomes_an_area(self) -> None:
        index = build_entity_index(
            [
                {
                    "tool": "research",
                    "payload": {
                        "entity_name": "Canggu",
                        "entity_key": _CANGGU_ROW.geo_key,
                    },
                }
            ]
        )
        assert index[0][1].kind == "area"
        assert index[0][1].uri == _CANGGU_URI

    def test_research_place_key_becomes_a_venue(self) -> None:
        index = build_entity_index(
            [
                {
                    "tool": "research",
                    "payload": {"entity_name": "Luigi's", "entity_key": "place:p9"},
                }
            ]
        )
        assert index[0][1].kind == "venue"
        assert index[0][1].uri == "kebi://venue/p9"

    def test_very_short_names_are_never_linked(self) -> None:
        # Linking two-letter names turns ordinary words into taps.
        assert (
            build_entity_index([_place_result(candidates=[_candidate("p1", "Om")])])
            == []
        )

    def test_a_venue_carries_the_icon_off_its_catalog_row(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis", icon="🍕")])]
        )
        assert dict(index)["Luigis"].icon == "🍕"

    def test_a_venue_without_an_icon_leaves_it_unset(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis")])]
        )
        assert dict(index)["Luigis"].icon is None

    def test_a_junk_icon_on_the_row_is_dropped(self) -> None:
        # Icons are model output; an ASCII word is not an emoji.
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis", icon="pizza")])]
        )
        assert dict(index)["Luigis"].icon is None

    async def test_area_entities_mint_icon_less_even_when_the_resolver_picked_one(
        self,
    ) -> None:
        # Amends ADR-146: per-turn resolver picks never ride entities — a chip
        # must match the screen its tap opens, so icons are re-read from the
        # stored rows at attach time (EntityIconRefresher). The row is the
        # only picker; here there is no row, so there is no icon.
        index = build_entity_index(
            [],
            await _wl_pairs({**_CANGGU, "city_icon": "🏝️", "neighborhood_icon": "🏄"}),
        )
        by_alias = dict(index)
        assert by_alias["Canggu"].icon is None
        assert by_alias["Badung"].icon is None

    async def test_a_venue_wins_a_same_named_area(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Canggu")])],
            await _wl_pairs(_CANGGU),
        )
        assert dict(index)["Canggu"].kind == "venue"


class TestLinkify:
    def test_wraps_a_known_name(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis")])]
        )
        text, entities = linkify("tonight is Luigis night", index)
        assert text == "tonight is [Luigis](kebi://venue/p1) night"
        assert [e.key for e in entities] == ["p1"]

    def test_only_the_first_mention_is_linked(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis")])]
        )
        text, entities = linkify("Luigis is great, go to Luigis", index)
        assert text == "[Luigis](kebi://venue/p1) is great, go to Luigis"
        assert len(entities) == 1

    def test_matching_is_case_insensitive_but_keeps_the_written_form(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis")])]
        )
        text, _ = linkify("go to luigis", index)
        assert text == "go to [luigis](kebi://venue/p1)"

    def test_longer_name_wins_over_a_contained_one(self) -> None:
        index = build_entity_index(
            [
                _place_result(
                    candidates=[
                        _candidate("p1", "Luigis"),
                        _candidate("p2", "Luigis Hot Pizza"),
                    ]
                )
            ]
        )
        text, entities = linkify("try Luigis Hot Pizza tonight", index)
        assert text == "try [Luigis Hot Pizza](kebi://venue/p2) tonight"
        assert [e.key for e in entities] == ["p2"]

    def test_a_name_inside_a_word_is_not_a_mention(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Finns")])]
        )
        text, entities = linkify("Finnsland is elsewhere", index)
        assert text == "Finnsland is elsewhere"
        assert entities == []

    async def test_venues_and_areas_link_in_one_pass(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Luigis")])],
            await _wl_pairs(_CANGGU),
        )
        text, entities = linkify("Luigis is the Monday move in Canggu", index)
        assert text == (
            f"[Luigis](kebi://venue/p1) is the Monday move in [Canggu]({_CANGGU_URI})"
        )
        assert [e.kind for e in entities] == ["venue", "area"]

    def test_nothing_retrieved_is_a_byte_identical_no_op(self) -> None:
        text, entities = linkify("just some prose", [])
        assert text == "just some prose"
        assert entities == []

    def test_regex_metacharacters_in_a_name_are_literal(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Mama (Deli) + Co.")])]
        )
        text, entities = linkify("go to Mama (Deli) + Co. tonight", index)
        assert "[Mama (Deli) + Co.](kebi://venue/p1)" in text
        assert len(entities) == 1


class TestTurnRecommendationId:
    def test_first_place_tool_result_wins(self) -> None:
        results = [
            {"tool": "research", "payload": {"entity_key": "id"}},
            _place_result(recommendation_id="rec-a"),
            _place_result(tool="suggest_places", recommendation_id="rec-b"),
        ]
        assert turn_recommendation_id(results) == "rec-a"

    def test_none_when_no_place_tool_ran(self) -> None:
        assert turn_recommendation_id([{"tool": "research", "payload": {}}]) is None


class TestEveryPlaceToolIsIndexed:
    """A `ConsultResult`-returning tool missing from `_PLACE_TOOLS` silently
    loses its links — the agent names its places and they arrive as plain
    text. This caught `find_known` shipping unlinked."""

    def test_the_index_covers_every_consult_family_tool(self) -> None:
        from kebi.core.agent.entity_links import _PLACE_TOOLS

        assert {
            "find_saved",
            "suggest_places",
            "discover_places",
            "find_known",
        } == _PLACE_TOOLS

    def test_the_recall_signal_covers_the_same_set(self) -> None:
        # `surfaced_places` gates the home recall list (ADR-110); a place tool
        # missing there means a real turn never enters it.
        from kebi.core.agent.entity_links import _PLACE_TOOLS as LINK_TOOLS
        from kebi.core.chat.service import _PLACE_TOOLS as RECALL_TOOLS

        assert LINK_TOOLS == RECALL_TOOLS

    def test_find_known_candidates_become_venue_links(self) -> None:
        index = build_entity_index(
            [_place_result(tool="find_known", candidates=[_candidate("p1", "Luigis")])]
        )
        text, entities = linkify("tonight is Luigis night", index)
        assert text == "tonight is [Luigis](kebi://venue/p1) night"
        assert [e.key for e in entities] == ["p1"]

    def test_find_known_supplies_the_recommendation_id(self) -> None:
        results = [_place_result(tool="find_known", recommendation_id="rec-known")]
        assert turn_recommendation_id(results) == "rec-known"


class TestAreaNameCollisions:
    """An area name inside a longer proper noun is not a mention of the area.

    Regression for a live capture where "Jl. Raya Canggu" linked its street
    name to the Canggu neighbourhood sheet — a link that resolves to the wrong
    screen, which costs more trust than no link at all.
    """

    async def _index(self) -> list[tuple[str, Any]]:
        return build_entity_index([], await _wl_pairs(_CANGGU))

    async def test_an_area_inside_a_street_name_is_not_linked(self) -> None:
        text, entities = linkify(
            "the nearest is on Jl. Raya Canggu", await self._index()
        )
        assert text == "the nearest is on Jl. Raya Canggu"
        assert entities == []

    async def test_an_area_inside_a_shouted_venue_name_is_not_linked(self) -> None:
        text, _ = linkify("BNI CANGGU is closest", await self._index())
        assert text == "BNI CANGGU is closest"

    async def test_an_area_after_a_provider_separator_is_not_linked(self) -> None:
        text, _ = linkify("Motel Mexicola | Canggu is open", await self._index())
        assert text == "Motel Mexicola | Canggu is open"

    async def test_a_real_area_mention_still_links(self) -> None:
        text, entities = linkify("a good night to be in Canggu", await self._index())
        assert text == f"a good night to be in [Canggu]({_CANGGU_URI})"
        assert [e.kind for e in entities] == ["area"]

    async def test_an_area_opening_a_sentence_still_links(self) -> None:
        text, _ = linkify("Canggu is busy tonight", await self._index())
        assert text.startswith(f"[Canggu]({_CANGGU_URI})")

    async def test_an_area_after_a_sentence_end_still_links(self) -> None:
        text, _ = linkify("thats sorted. Canggu is busy.", await self._index())
        assert f"[Canggu]({_CANGGU_URI})" in text


class TestDisplayNames:
    """Provider strings never reach the user (ADR-137 voice rules)."""

    def test_the_entity_carries_the_cleaned_name(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "BNI CANGGU")])]
        )
        assert {e.name for _, e in index} == {"BNI Canggu"}

    def test_both_spellings_resolve_to_the_same_tap(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Motel Mexicola | Canggu")])]
        )
        for written in ("Motel Mexicola", "Motel Mexicola | Canggu"):
            text, entities = linkify(f"go to {written} tonight", index)
            assert "kebi://venue/p1" in text
            assert entities[0].name == "Motel Mexicola"


class TestVoiceNormalization:
    """Typography the prompt forbids is enforced in code, because the rule
    kept losing to the rest of the prompt across live samples. These carry no
    meaning, so normalising them is safe; judgment stays in the prompt."""

    def test_an_em_dash_between_clauses_becomes_a_comma(self) -> None:
        assert (
            normalize_voice("the anchor is Luigi's — monday is their night")
            == "the anchor is Luigi's, monday is their night"
        )

    def test_an_en_dash_is_treated_the_same(self) -> None:
        assert normalize_voice("runs every day – easy end") == (
            "runs every day, easy end"
        )

    def test_a_dash_without_surrounding_spaces_is_caught(self) -> None:
        assert normalize_voice("fees—so skip it") == "fees, so skip it"

    def test_bold_markers_are_stripped(self) -> None:
        assert normalize_voice("**skip for tonight:**") == "skip for tonight:"

    def test_arrows_become_the_word(self) -> None:
        assert normalize_voice("Luigi → Old Man") == "Luigi then Old Man"

    def test_a_dash_against_punctuation_leaves_no_doubled_comma(self) -> None:
        assert normalize_voice("good spot —, really") == "good spot, really"

    def test_ordinary_prose_is_untouched(self) -> None:
        text = "monday is a good night to be in canggu, here's how i'd play it"
        assert normalize_voice(text) == text

    def test_hyphens_in_words_survive(self) -> None:
        assert normalize_voice("a wed-to-sat spot") == "a wed-to-sat spot"

    def test_normalisation_runs_before_linking_so_names_still_match(self) -> None:
        index = build_entity_index(
            [_place_result(candidates=[_candidate("p1", "Old Man's")])]
        )
        text, entities = linkify(
            normalize_voice("go to Old Man's — it runs every night"), index
        )
        assert text == "go to [Old Man's](kebi://venue/p1), it runs every night"
        assert len(entities) == 1


class TestShortFormsMustBeDistinctive:
    """A one-word short form has to stand alone as a name.

    Regression from a live answer where "After Rock, Bali" contributed the
    short form "After", so the ordinary word in "what kind of night you're
    after" became a tap on a bar the sentence was not about — the same
    wrong-destination failure as linking an area inside a street name.
    """

    def _index(self) -> list[tuple[str, Any]]:
        return build_entity_index(
            [
                _place_result(
                    candidates=[
                        _candidate("p1", "After Rock, Bali"),
                        _candidate("p2", "Luigi's Hot Pizza Canggu"),
                    ]
                )
            ]
        )

    def test_a_common_word_never_becomes_a_link(self) -> None:
        text, entities = linkify("depends what you're after tonight", self._index())
        assert text == "depends what you're after tonight"
        assert entities == []

    def test_the_full_name_still_links(self) -> None:
        text, _ = linkify("try After Rock later", self._index())
        assert "[After Rock](kebi://venue/p1)" in text

    def test_a_distinctive_one_word_short_form_still_links(self) -> None:
        text, _ = linkify("start at Luigi's", self._index())
        assert "[Luigi's](kebi://venue/p2)" in text

    def test_the_trailing_area_is_dropped_from_the_display_name(self) -> None:
        names = {e.name for _, e in self._index()}
        assert "After Rock" in names
        assert "After Rock, Bali" not in names


def _web_result(findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tool": "web_search",
        "tool_call_id": "call-w1",
        "payload": {"query": "world cup schedule", "findings": findings},
    }


def _finding(domain: str | None, url: str | None) -> dict[str, Any]:
    return {"text": "some finding text", "source": domain, "age": "2d", "url": url}


class TestWebEntities:
    def test_web_findings_become_web_entities(self) -> None:
        index = build_entity_index(
            [_web_result([_finding("fifa.com", "https://fifa.com/schedule")])]
        )
        by_alias = dict(index)
        entity = by_alias["fifa.com"]
        assert entity.kind == "web"
        assert entity.key == "https://fifa.com/schedule"
        assert entity.uri == f"kebi://web/{encode_web_url('https://fifa.com/schedule')}"
        assert entity.name == "fifa.com"
        assert entity.icon == "🌐"

    def test_one_entity_per_domain_first_page_wins(self) -> None:
        # Findings arrive provider-ranked; the top page from a domain is the
        # one the answer leaned on, so its URL is the one the tap opens.
        index = build_entity_index(
            [
                _web_result(
                    [
                        _finding("fifa.com", "https://fifa.com/schedule"),
                        _finding("fifa.com", "https://fifa.com/tickets"),
                        _finding("bbc.com", "https://bbc.com/sport"),
                    ]
                )
            ]
        )
        webs = [e for _, e in index if e.kind == "web"]
        assert len(webs) == 2
        assert dict(index)["fifa.com"].key == "https://fifa.com/schedule"
        assert dict(index)["bbc.com"].key == "https://bbc.com/sport"

    def test_findings_missing_domain_or_url_are_skipped(self) -> None:
        index = build_entity_index(
            [
                _web_result(
                    [
                        _finding(None, "https://fifa.com/schedule"),
                        _finding("bbc.com", None),
                        _finding("mailto.example", "mailto:x@example.com"),
                    ]
                )
            ]
        )
        assert index == []

    def test_linkify_wraps_a_domain_mention(self) -> None:
        index = build_entity_index(
            [_web_result([_finding("fifa.com", "https://fifa.com/schedule")])]
        )
        text, entities = linkify(
            "the group stage starts june 11, per the schedule on fifa.com.", index
        )
        uri = f"kebi://web/{encode_web_url('https://fifa.com/schedule')}"
        assert f"[fifa.com]({uri})" in text
        assert [e.kind for e in entities] == ["web"]

    def test_uncited_source_contributes_no_entity_to_the_answer(self) -> None:
        # Read-but-uncited pages stay invisible: linkify only surfaces what
        # the prose names, so the entity list vouches for the citation.
        index = build_entity_index(
            [
                _web_result(
                    [
                        _finding("fifa.com", "https://fifa.com/schedule"),
                        _finding("bbc.com", "https://bbc.com/sport"),
                    ]
                )
            ]
        )
        text, entities = linkify("the schedule on fifa.com says june 11.", index)
        assert [e.name for e in entities] == ["fifa.com"]
        assert "bbc.com" not in text
