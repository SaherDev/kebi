"""Tests for the web-search provider seam (ADR-145).

The contract these lock is "never raise, degrade to empty" — every consumer
above treats `[]` as "answer from what you know", so a provider that throws
would turn a nuance into a failed turn.
"""

from __future__ import annotations

from typing import Any

import httpx

from kebi.providers.web_search import (
    BraveWebSearchProvider,
    CachedWebSearchProvider,
    NullWebSearchProvider,
    WebResult,
    WebSearchProvider,
)
from kebi.providers.web_search.cached import _cache_key

# --- the null provider ------------------------------------------------------


async def test_the_null_provider_returns_nothing() -> None:
    assert await NullWebSearchProvider().search("world cup", count=5) == []


def test_the_null_provider_satisfies_the_protocol() -> None:
    provider: WebSearchProvider = NullWebSearchProvider()
    assert isinstance(provider, WebSearchProvider)


# --- Brave -----------------------------------------------------------------


def _brave(handler: Any) -> BraveWebSearchProvider:
    return BraveWebSearchProvider(
        api_key="test-key",
        base_url="https://api.search.brave.com",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ok(payload: dict[str, Any]) -> Any:
    return lambda request: httpx.Response(200, json=payload)


async def test_a_web_result_becomes_a_web_result() -> None:
    provider = _brave(
        _ok(
            {
                "web": {
                    "results": [
                        {
                            "title": "Bali visa on arrival",
                            "url": "https://example.com/visa",
                            "description": "The fee is 500,000 IDR.",
                            "meta_url": {"netloc": "example.com"},
                            "age": "3 months ago",
                        }
                    ]
                }
            }
        )
    )
    results = await provider.search("bali visa", count=5)
    assert results == [
        WebResult(
            title="Bali visa on arrival",
            url="https://example.com/visa",
            snippet="The fee is 500,000 IDR.",
            site="example.com",
            age="3 months ago",
        )
    ]


async def test_news_leads_the_web_cluster() -> None:
    """Dated items answer "when is it" and "is it still on", which is most of
    what web search was added for."""
    provider = _brave(
        _ok(
            {
                "web": {
                    "results": [
                        {
                            "title": "General",
                            "url": "https://a.example/1",
                            "description": "background",
                        }
                    ]
                },
                "news": {
                    "results": [
                        {
                            "title": "Dated",
                            "url": "https://b.example/2",
                            "description": "happening friday",
                        }
                    ]
                },
            }
        )
    )
    results = await provider.search("festival", count=5)
    assert [r.title for r in results] == ["Dated", "General"]


async def test_the_same_page_in_both_clusters_appears_once() -> None:
    hit = {
        "title": "Same",
        "url": "https://same.example/x",
        "description": "one page",
    }
    provider = _brave(_ok({"news": {"results": [hit]}, "web": {"results": [hit]}}))
    assert len(await provider.search("q", count=5)) == 1


async def test_extra_snippets_are_folded_into_the_text() -> None:
    provider = _brave(
        _ok(
            {
                "web": {
                    "results": [
                        {
                            "title": "T",
                            "url": "https://e.example/1",
                            "description": "first.",
                            "extra_snippets": ["second.", "third."],
                        }
                    ]
                }
            }
        )
    )
    (result,) = await provider.search("q", count=5)
    assert result.snippet == "first. second. third."


async def test_query_term_markup_is_stripped() -> None:
    """Brave bolds the matched terms. The chat contract is text plus
    `kebi://` links only — raw HTML has no business reaching a client."""
    provider = _brave(
        _ok(
            {
                "web": {
                    "results": [
                        {
                            "title": "<strong>Bali</strong> ferries",
                            "url": "https://e.example/1",
                            "description": "the <strong>ferry</strong> runs daily",
                        }
                    ]
                }
            }
        )
    )
    (result,) = await provider.search("ferry", count=5)
    assert "<strong>" not in result.title
    assert result.snippet == "the ferry runs daily"


async def test_a_result_with_no_text_is_dropped() -> None:
    provider = _brave(
        _ok({"web": {"results": [{"title": "T", "url": "https://e.example/1"}]}})
    )
    assert await provider.search("q", count=5) == []


async def test_an_http_error_degrades_to_empty() -> None:
    provider = _brave(lambda request: httpx.Response(429, json={"error": "rate"}))
    assert await provider.search("q", count=5) == []


async def test_a_transport_error_degrades_to_empty() -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert await _brave(_boom).search("q", count=5) == []


async def test_a_garbage_body_degrades_to_empty() -> None:
    provider = _brave(lambda request: httpx.Response(200, json=["not", "a", "dict"]))
    assert await provider.search("q", count=5) == []


async def test_the_human_freshness_word_becomes_the_wire_code() -> None:
    seen: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={})

    await _brave(_capture).search("q", count=5, freshness="week", country="id")
    assert seen["freshness"] == "pw"
    assert seen["country"] == "ID"


async def test_an_unknown_freshness_word_is_simply_omitted() -> None:
    seen: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={})

    await _brave(_capture).search("q", count=5, freshness="fortnight")
    assert "freshness" not in seen


async def test_the_subscription_token_is_sent() -> None:
    seen: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Subscription-Token")
        return httpx.Response(200, json={})

    await _brave(_capture).search("q", count=5)
    assert seen["token"] == "test-key"


# --- the cache -------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.sets += 1
        self.store[key] = value


class _CountingProvider:
    def __init__(self, results: list[WebResult]) -> None:
        self.results = results
        self.calls = 0

    async def search(self, query: str, **kw: Any) -> list[WebResult]:
        self.calls += 1
        return self.results


_HIT = WebResult(title="T", url="https://e.example/1", snippet="text")


async def test_a_repeat_question_does_not_hit_the_provider() -> None:
    """The firing rule is permissive by design, so this is what keeps it
    affordable."""
    inner = _CountingProvider([_HIT])
    cached = CachedWebSearchProvider(inner, _FakeRedis(), ttl_seconds=60)
    first = await cached.search("raves in bali", count=5)
    second = await cached.search("raves in bali", count=5)
    assert first == second == [_HIT]
    assert inner.calls == 1


async def test_the_key_ignores_casing_and_spacing() -> None:
    inner = _CountingProvider([_HIT])
    cached = CachedWebSearchProvider(inner, _FakeRedis(), ttl_seconds=60)
    await cached.search("Raves  In Bali", count=5)
    await cached.search("raves in bali", count=5)
    assert inner.calls == 1


async def test_freshness_is_part_of_the_question() -> None:
    """ "raves in bali" and "raves in bali this month" are different questions;
    serving one for the other is how a cache starts lying about dates."""
    assert _cache_key("q", 5, None, "id") != _cache_key("q", 5, "month", "id")
    assert _cache_key("q", 5, "day", "id") != _cache_key("q", 5, "day", "th")


async def test_an_empty_result_is_not_cached() -> None:
    """Empty usually means an outage or a missing key. Caching it would freeze
    that outage in for the whole TTL."""
    inner = _CountingProvider([])
    redis = _FakeRedis()
    cached = CachedWebSearchProvider(inner, redis, ttl_seconds=60)
    await cached.search("q", count=5)
    await cached.search("q", count=5)
    assert inner.calls == 2
    assert redis.sets == 0


async def test_a_dead_cache_degrades_to_the_live_provider() -> None:
    class _BrokenRedis:
        async def get(self, key: str) -> str | None:
            raise ConnectionError("redis down")

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise ConnectionError("redis down")

    inner = _CountingProvider([_HIT])
    cached = CachedWebSearchProvider(inner, _BrokenRedis(), ttl_seconds=60)
    assert await cached.search("q", count=5) == [_HIT]


async def test_a_malformed_cache_entry_evicts_rather_than_crashes() -> None:
    inner = _CountingProvider([_HIT])
    redis = _FakeRedis()
    cached = CachedWebSearchProvider(inner, redis, ttl_seconds=60)
    redis.store[_cache_key("q", 5, None, None)] = '[{"shape": "changed"}]'
    assert await cached.search("q", count=5) == [_HIT]
