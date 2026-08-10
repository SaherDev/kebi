"""Tests for the web-finding harvester (ADR-145).

Two properties carry the whole design: dated facts must never become claims,
and a claim must never be keyed to an area it does not name.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kebi.core.knowledge.schemas import ResolvedGeo
from kebi.core.knowledge.web_harvester import (
    WebKnowledgeHarvester,
    _WebClaim,
    _WebHarvestResponse,
    is_entry_rule,
)
from kebi.core.web.models import WebFinding, WebSearchResult


def _result(**kw: Any) -> WebSearchResult:
    base: dict[str, Any] = {
        "query": "atm fees Canggu",
        "findings": [WebFinding(text="BNI charges no withdrawal fee.")],
        "country_code": "id",
        "city": "Badung",
        "neighborhood": "Canggu",
    }
    base.update(kw)
    return WebSearchResult(**base)


def _claim(**kw: Any) -> _WebClaim:
    base: dict[str, Any] = {
        "scope": "neighborhood",
        "entity_name": "Canggu",
        "claim": "BNI ATMs here charge no withdrawal fee.",
        "tags": ["no_fee_atm"],
        "confidence": 0.7,
        "durable": True,
    }
    base.update(kw)
    return _WebClaim(**base)


def _harvester(claims: list[_WebClaim], geocoder: Any = None) -> WebKnowledgeHarvester:
    client = MagicMock()
    client.extract = AsyncMock(return_value=_WebHarvestResponse(claims=claims))
    return WebKnowledgeHarvester(client, geocoder or MagicMock())


# --- provenance ------------------------------------------------------------


def test_it_declares_itself_a_web_search_producer() -> None:
    harvester = _harvester([])
    assert harvester.source_type == "web_search"
    assert harvester.confidence_floor == 0.25


# --- durability ------------------------------------------------------------


async def test_a_durable_area_fact_becomes_a_claim() -> None:
    claims = await _harvester([_claim()]).harvest(_result())
    assert len(claims) == 1
    assert claims[0].geo == ResolvedGeo(
        country_code="id", city="Badung", neighborhood="Canggu"
    )


async def test_a_dated_fact_is_answered_and_forgotten() -> None:
    """A claims store full of expired schedules is worse than one without
    them — this is the line between the two."""
    claims = await _harvester(
        [_claim(claim="The festival runs on the 12th.", durable=False)]
    ).harvest(_result())
    assert claims == []


async def test_the_durable_ones_survive_alongside_the_dated_ones() -> None:
    claims = await _harvester(
        [
            _claim(claim="Cash is expected at warungs."),
            _claim(claim="Tickets are 300k this weekend.", durable=False),
        ]
    ).harvest(_result())
    assert [c.claim for c in claims] == ["Cash is expected at warungs."]


# --- entry rules never bank (ADR-154) --------------------------------------


async def test_an_entry_rule_is_dropped_even_when_marked_durable() -> None:
    """The model marks these durable because they read stable, and they are not.

    Visa rules change without notice and differ by passport, so a stored one
    is later asserted either after it expired or to the wrong traveller —
    and being wrong about one costs someone a flight. The prompt forbids
    them; this is the backstop for when it slips.
    """
    claims = await _harvester(
        [
            _claim(
                claim="UAE citizens can enter Indonesia visa-free for 30 days.",
                entity_name="Indonesia",
                scope="country",
                durable=True,
            )
        ]
    ).harvest(_result())
    assert claims == []


async def test_an_entry_rule_does_not_take_the_real_claims_with_it() -> None:
    """The guard drops one claim, not the whole harvest."""
    claims = await _harvester(
        [
            _claim(claim="Cash is expected at warungs."),
            _claim(claim="Your passport must be valid six months on arrival."),
        ]
    ).harvest(_result())
    assert [c.claim for c in claims] == ["Cash is expected at warungs."]


def test_is_entry_rule_catches_the_vocabulary() -> None:
    for text in (
        "Visa-free entry is 30 days.",
        "A PASSPORT must be valid for six months.",
        "Immigration will ask for an onward ticket.",
        "The residence permit takes four weeks.",
        "Fill the arrival card before landing.",
    ):
        assert is_entry_rule(text), text


def test_is_entry_rule_leaves_ordinary_local_knowledge_alone() -> None:
    """Over-dropping is the safe direction, but not at any price.

    Local custom and etiquette are the harvester's whole purpose, so the
    vocabulary deliberately omits "customs" — it collides with them.
    """
    for text in (
        "Cash is expected at warungs.",
        "It is local custom to remove your shoes.",
        "Customs here favour a small offering at the door.",
        "The ferry needs booking a day ahead.",
        "Wet season runs November to March.",
    ):
        assert not is_entry_rule(text), text


# --- key verification ------------------------------------------------------


async def test_a_neighbourhood_claim_naming_somewhere_else_is_dropped() -> None:
    """Verified-or-drop (ADR-126). The alternative is filing a fact about
    Seminyak under Canggu because that is where the conversation was."""
    claims = await _harvester([_claim(entity_name="Seminyak")]).harvest(_result())
    assert claims == []


async def test_a_city_claim_naming_the_turns_city_keys_to_it() -> None:
    claims = await _harvester([_claim(scope="city", entity_name="Badung")]).harvest(
        _result()
    )
    assert claims[0].geo == ResolvedGeo(country_code="id", city="Badung")


async def test_the_turns_geography_outranks_the_models_scope_label() -> None:
    """Regression, found live: this dropped 100% of claims.

    The prompt's taxonomy calls any populated area a "city", so a fact about
    Canggu comes back scoped `city` — while the turn resolved Canggu as the
    *neighbourhood* of Badung. Trusting the label meant matching "Canggu"
    against "Badung", failing, falling through to a geocode that cannot
    verify Canggu as a city either, and dropping the claim. Silent, and total.
    """
    claims = await _harvester([_claim(scope="city", entity_name="Canggu")]).harvest(
        _result()
    )
    assert claims[0].scope == "neighborhood"
    assert claims[0].geo == ResolvedGeo(
        country_code="id", city="Badung", neighborhood="Canggu"
    )


async def test_the_label_still_decides_when_no_name_matches() -> None:
    """The override is name-driven, not a blanket demotion — a claim about
    somewhere else must still take the geocoder path."""
    import kebi.core.knowledge.web_harvester as module

    original = module.EntityGeoResolver
    module.EntityGeoResolver = lambda _g: MagicMock(  # type: ignore[assignment]
        resolve_city=AsyncMock(return_value=None)
    )
    try:
        # scope=neighborhood + unmatched name → dropped, never geocoded.
        dropped = await _harvester(
            [_claim(scope="neighborhood", entity_name="Seminyak")]
        ).harvest(_result())
    finally:
        module.EntityGeoResolver = original  # type: ignore[assignment]
    assert dropped == []


async def test_a_city_claim_naming_another_city_is_geocoded() -> None:
    geocoder = MagicMock()
    harvester = _harvester([_claim(scope="city", entity_name="Ubud")], geocoder)
    resolved = ResolvedGeo(country_code="id", city="Ubud")

    async def _resolve_city(name: str, country_code: str) -> ResolvedGeo | None:
        return resolved if name == "Ubud" else None

    import kebi.core.knowledge.web_harvester as module

    original = module.EntityGeoResolver
    module.EntityGeoResolver = lambda _g: MagicMock(  # type: ignore[assignment]
        resolve_city=AsyncMock(side_effect=_resolve_city)
    )
    try:
        claims = await harvester.harvest(_result())
    finally:
        module.EntityGeoResolver = original  # type: ignore[assignment]
    assert claims[0].geo == resolved


async def test_an_unresolvable_city_claim_is_dropped() -> None:
    import kebi.core.knowledge.web_harvester as module

    original = module.EntityGeoResolver
    module.EntityGeoResolver = lambda _g: MagicMock(  # type: ignore[assignment]
        resolve_city=AsyncMock(return_value=None)
    )
    try:
        claims = await _harvester(
            [_claim(scope="city", entity_name="Nowhereville")]
        ).harvest(_result())
    finally:
        module.EntityGeoResolver = original  # type: ignore[assignment]
    assert claims == []


async def test_no_country_code_leaves_a_neighbourhood_claim_unkeyable() -> None:
    claims = await _harvester([_claim()]).harvest(_result(country_code=None))
    assert claims == []


# --- best effort -----------------------------------------------------------


async def test_no_findings_means_no_llm_call() -> None:
    client = MagicMock()
    client.extract = AsyncMock()
    harvester = WebKnowledgeHarvester(client, MagicMock())
    assert await harvester.harvest(_result(findings=[])) == []
    client.extract.assert_not_awaited()


async def test_an_llm_failure_costs_the_enrichment_and_nothing_else() -> None:
    client = MagicMock()
    client.extract = AsyncMock(side_effect=RuntimeError("model down"))
    harvester = WebKnowledgeHarvester(client, MagicMock())
    assert await harvester.harvest(_result()) == []


# --- the schema the model must fill ----------------------------------------


def test_there_is_no_place_scope() -> None:
    """A finding names venues in prose but carries no catalog id, so web
    knowledge keys to areas only."""
    scope = _WebClaim.model_fields["scope"]
    assert set(scope.annotation.__args__) == {  # type: ignore[union-attr]
        "neighborhood",
        "city",
        "country",
    }
