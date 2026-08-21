"""Usage extraction from provider SDK objects → Langfuse-shaped dicts (ADR-172)."""

from __future__ import annotations

from types import SimpleNamespace

from kebi.core.agent.graph import _ai_message_usage
from kebi.providers.llm import _anthropic_usage, _openai_usage


def test_openai_usage_splits_cached_tokens_out_of_input() -> None:
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=200,
        total_tokens=1200,
        prompt_tokens_details=SimpleNamespace(cached_tokens=600),
    )
    assert _openai_usage(usage) == {
        "input": 400,
        "output": 200,
        "total": 1200,
        "cache_read_input_tokens": 600,
    }


def test_openai_usage_without_cache_details() -> None:
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        prompt_tokens_details=None,
    )
    assert _openai_usage(usage) == {"input": 100, "output": 50, "total": 150}


def test_openai_usage_none() -> None:
    assert _openai_usage(None) is None


def test_anthropic_usage_forwards_cache_buckets() -> None:
    usage = SimpleNamespace(
        input_tokens=300,
        output_tokens=80,
        cache_read_input_tokens=8000,
        cache_creation_input_tokens=500,
    )
    assert _anthropic_usage(usage) == {
        "input": 300,
        "output": 80,
        "total": 8880,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 500,
    }


def test_ai_message_usage_propagates_langchain_cache_details() -> None:
    """LangChain's input_tokens INCLUDES cached tokens; kebi's convention
    counts uncached input under `input` and forwards the cache buckets —
    that's what makes prompt-cache hit rate visible (was dropped before)."""
    msg = SimpleNamespace(
        usage_metadata={
            "input_tokens": 9000,
            "output_tokens": 150,
            "total_tokens": 9150,
            "input_token_details": {"cache_read": 8000, "cache_creation": 500},
        }
    )
    assert _ai_message_usage(msg) == {
        "input": 500,
        "output": 150,
        "total": 9150,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 500,
    }


def test_ai_message_usage_without_details_unchanged() -> None:
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    )
    assert _ai_message_usage(msg) == {"input": 100, "output": 20, "total": 120}


def test_ai_message_usage_missing_metadata() -> None:
    assert _ai_message_usage(SimpleNamespace()) == {}
