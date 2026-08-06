"""Tests for WebKnowledgeService — search shaped for an answer (ADR-145)."""

from __future__ import annotations

from typing import Any

from kebi.core.agent.location import WorkingLocation
from kebi.core.config import WebSearchToolConfig
from kebi.core.web.service import WebKnowledgeService
from kebi.providers.web_search import WebResult

_CANGGU = WorkingLocation(
    country="Indonesia",
    country_code="id",
    city="Badung",
    neighborhood="Canggu",
    lat=-8.65,
    lng=115.13,
)


class _Spy:
    def __init__(self, results: list[WebResult] | None = None) -> None:
        self.results = results or []
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, **kw: Any) -> list[WebResult]:
        self.calls.append({"query": query, **kw})
        return self.results


def _service(spy: _Spy, **cfg: Any) -> WebKnowledgeService:
    return WebKnowledgeService(provider=spy, config=WebSearchToolConfig(**cfg))


def _hit(**kw: Any) -> WebResult:
    base = {"title": "T", "url": "https://e.example/1", "snippet": "body"}
    base.update(kw)
    return WebResult(**base)


# --- localisation ----------------------------------------------------------


async def test_the_turn_location_is_appended_to_the_query() -> None:
    spy = _Spy([_hit()])
    await _service(spy).search(query="atm fees", working=_CANGGU)
    assert spy.calls[0]["query"] == "atm fees Canggu"


async def test_the_country_is_passed_for_index_localisation() -> None:
    spy = _Spy([_hit()])
    await _service(spy).search(query="atm fees", working=_CANGGU)
    assert spy.calls[0]["country"] == "id"


async def test_an_area_the_query_already_names_is_not_repeated() -> None:
    """Search engines weight repetition; "canggu canggu atm" ranks
    differently than intended."""
    spy = _Spy([_hit()])
    await _service(spy).search(query="canggu atm fees", working=_CANGGU)
    assert spy.calls[0]["query"] == "canggu atm fees"


async def test_naming_any_level_of_the_area_suppresses_the_append() -> None:
    """Regression: walking the levels and appending the first one *absent* is
    the obvious implementation and it is wrong. "canggu atm fees" would pick
    up the administrative city and search "canggu atm fees Badung" — nobody
    writing about Canggu says Badung, so that query is worse than either name
    alone."""
    spy = _Spy([_hit()])
    await _service(spy).search(query="world cup 2026 indonesia", working=_CANGGU)
    assert spy.calls[0]["query"] == "world cup 2026 indonesia"


async def test_no_working_location_searches_the_query_verbatim() -> None:
    spy = _Spy([_hit()])
    await _service(spy).search(query="world cup schedule")
    assert spy.calls[0]["query"] == "world cup schedule"
    assert spy.calls[0]["country"] is None


# --- limits ----------------------------------------------------------------


async def test_the_agent_cannot_ask_for_more_than_the_cap() -> None:
    spy = _Spy([_hit()])
    await _service(spy, default_limit=5, max_limit=8).search(query="q", limit=99)
    assert spy.calls[0]["count"] == 8


async def test_an_omitted_limit_uses_the_default() -> None:
    spy = _Spy([_hit()])
    await _service(spy, default_limit=5, max_limit=8).search(query="q")
    assert spy.calls[0]["count"] == 5


# --- shaping ---------------------------------------------------------------


async def test_a_finding_carries_the_title_and_the_snippet() -> None:
    spy = _Spy([_hit(title="Ferry times", snippet="runs at 8 and 4")])
    result = await _service(spy).search(query="ferry")
    assert result.findings[0].text == "Ferry times. runs at 8 and 4"


async def test_a_long_snippet_is_cut_at_a_word_boundary() -> None:
    spy = _Spy([_hit(title="T", snippet="word " * 200)])
    result = await _service(spy, snippet_max_chars=100).search(query="q")
    text = result.findings[0].text
    assert len(text) <= 101  # the ellipsis
    assert text.endswith("…")
    assert "wor…" not in text


async def test_syndicated_duplicates_collapse() -> None:
    """The same lede across a dozen domains reads to the model as a dozen
    sources agreeing."""
    lede = "The festival returns to the south coast for its tenth year running"
    spy = _Spy(
        [
            _hit(title="A", snippet=lede, url="https://a.example/1"),
            _hit(title="A", snippet=lede, url="https://b.example/2"),
        ]
    )
    result = await _service(spy).search(query="festival")
    assert len(result.findings) == 1


async def test_the_url_is_kept_server_side() -> None:
    spy = _Spy([_hit(url="https://e.example/page")])
    result = await _service(spy).search(query="q")
    assert result.findings[0].url == "https://e.example/page"


# --- the empty case --------------------------------------------------------


async def test_nothing_found_is_a_named_outcome_not_a_bare_empty_list() -> None:
    """ "I couldn't check that" and "I checked and there's nothing" are
    different sentences; the agent can only write the right one if the
    difference survives to it."""
    result = await _service(_Spy([])).search(query="q")
    assert result.findings == []
    assert result.empty_reason == "no_results"


async def test_the_searched_area_rides_along_for_the_harvest() -> None:
    result = await _service(_Spy([_hit()])).search(query="q", working=_CANGGU)
    assert (result.country_code, result.city, result.neighborhood) == (
        "id",
        "Badung",
        "Canggu",
    )
