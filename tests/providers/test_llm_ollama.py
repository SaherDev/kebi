"""Tests for Ollama provider support in the LLM factory."""

from unittest.mock import patch

from kebi.core.config import AppConfig, _load_prompts, load_yaml_config
from kebi.providers.llm import (
    InstructorClient,
    OpenAILLMClient,
    get_instructor_client,
    get_llm,
)


def _prepare_raw_config() -> dict:
    """Load app.yaml and resolve prompt filenames to PromptConfig objects.

    Mirrors what ``get_config()`` does on first call (ADR-059) so test
    fixtures can construct ``AppConfig`` directly from the yaml dict.
    """
    data = load_yaml_config("app.yaml")
    data["prompts"] = _load_prompts(data.get("prompts") or {})
    return data


def _config_with_ollama() -> AppConfig:
    """Load real app.yaml and overlay ollama provider."""
    data = _prepare_raw_config()
    data.setdefault("providers", {})["ollama"] = {
        "base_url": "http://localhost:11434/v1"
    }
    return AppConfig(**data)


def _mock_config(provider: str, model: str) -> AppConfig:
    """Build a minimal AppConfig with intent_parser pointing at the given provider."""
    data = _prepare_raw_config()
    data["models"]["intent_parser"] = {
        "provider": provider,
        "model": model,
        "max_tokens": 512,
        "temperature": 0,
    }
    data.setdefault("providers", {})["ollama"] = {
        "base_url": "http://localhost:11434/v1"
    }
    return AppConfig(**data)


def test_appconfig_accepts_ollama_provider() -> None:
    """AppConfig must parse an ollama entry under providers without error."""
    cfg = _config_with_ollama()
    assert cfg.providers.ollama.base_url == "http://localhost:11434/v1"


def test_openai_llm_client_accepts_base_url() -> None:
    """OpenAILLMClient must accept base_url and pass it to AsyncOpenAI."""
    client = OpenAILLMClient(
        model="gemma4:e2b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    )
    assert client._client.base_url.host == "localhost"


def test_instructor_client_accepts_base_url() -> None:
    """InstructorClient must accept base_url and pass it to AsyncOpenAI."""
    client = InstructorClient(
        model="gemma4:e2b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
    )
    assert client._openai_client.base_url.host == "localhost"


def test_get_llm_returns_openai_client_for_ollama_provider() -> None:
    """get_llm('intent_parser') with provider=ollama returns OpenAILLMClient."""
    cfg = _mock_config("ollama", "gemma4:e2b")
    with (
        patch("kebi.providers.llm.get_config", return_value=cfg),
        patch("kebi.providers.llm.get_env"),
    ):
        client = get_llm("intent_parser")
    assert isinstance(client, OpenAILLMClient)
    assert client._client.base_url.host == "localhost"


def test_get_instructor_client_returns_instructor_for_ollama_provider() -> None:
    """get_instructor_client('intent_parser') with provider=ollama returns InstructorClient with JSON mode."""
    import instructor as _instructor

    cfg = _mock_config("ollama", "gemma4:e2b")
    with (
        patch("kebi.providers.llm.get_config", return_value=cfg),
        patch("kebi.providers.llm.get_env"),
    ):
        client = get_instructor_client("intent_parser")
    assert isinstance(client, InstructorClient)
    assert client._openai_client.base_url.host == "localhost"
    assert client._client.mode == _instructor.Mode.JSON
