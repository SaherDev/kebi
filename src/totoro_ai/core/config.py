"""Central config module — single source of truth for all app configuration.

Two singletons:
- get_config()   → AppConfig    from config/app.yaml (committed, non-secret)
- get_env()  → EnvConfig from .env → env vars (never committed)

All other modules import from here. Nobody calls load_yaml_config() directly.
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Low-level YAML loader (internal — use get_config / get_env instead)
# ---------------------------------------------------------------------------


def find_project_root() -> Path:
    """Walk up from this file until we find pyproject.toml."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise FileNotFoundError("Could not find project root (no pyproject.toml found)")


def load_yaml_config(name: str) -> dict[str, Any]:
    """Load a YAML config file from config/<name>. File must exist."""
    config_path = find_project_root() / "config" / name
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found at {config_path}. Check your working directory."
        )
    try:
        with config_path.open() as f:
            result = yaml.safe_load(f)
    except yaml.YAMLError as err:
        raise ValueError(f"Invalid YAML in {config_path}: {err}") from err
    if not isinstance(result, dict):
        raise ValueError(
            f"Expected a YAML mapping in {config_path}, got {type(result).__name__}"
        )
    return result


# ---------------------------------------------------------------------------
# AppConfig — non-secret config from config/app.yaml
# ---------------------------------------------------------------------------


class AppMeta(BaseModel):
    name: str
    description: str
    api_prefix: str


class LLMRoleConfig(BaseModel):
    provider: str
    model: str
    max_tokens: int = 1024
    temperature: float = 1.0


class OrchestratorOptionsConfig(BaseModel):
    """Multi-option orchestrator block (ADR-068).

    Shape in YAML:
        orchestrator:
          default: <option-key>
          <option-key>: { provider, model, max_tokens, temperature }
          <option-key>: { ... }

    Resolved at boot via `_resolve_orchestrator(raw, agent_model)`. Other
    roles keep the flat `LLMRoleConfig` shape — orchestrator is the only
    role with runtime selection right now.
    """

    default: str
    options: dict[str, LLMRoleConfig]

    @model_validator(mode="after")
    def _default_must_exist(self) -> "OrchestratorOptionsConfig":
        if self.default not in self.options:
            raise ValueError(
                f"orchestrator.default={self.default!r} not found in options "
                f"{sorted(self.options)}"
            )
        return self


def _split_orchestrator_block(raw_orch: dict[str, Any]) -> OrchestratorOptionsConfig:
    """Parse the YAML orchestrator block into OrchestratorOptionsConfig.

    `default` is the only reserved key; every other key is an option name
    mapping to an `LLMRoleConfig`-shaped dict.
    """
    if "default" not in raw_orch:
        raise ValueError(
            "models.orchestrator must define a 'default' key naming one of "
            "its option keys"
        )
    default = raw_orch["default"]
    options = {k: v for k, v in raw_orch.items() if k != "default"}
    return OrchestratorOptionsConfig(default=default, options=options)


def _resolve_orchestrator(
    raw_models: dict[str, Any], agent_model: str | None
) -> dict[str, Any]:
    """Resolve a `{default, <option>, ...}` orchestrator block to a flat
    `LLMRoleConfig` dict (ADR-068).

    No-op if the block is already flat (i.e. has top-level `provider`/`model`).
    Mutates `raw_models["orchestrator"]` in place and returns the same dict.

    - `agent_model` is None  → use `default`.
    - `agent_model` matches an option key → use that option.
    - `agent_model` is set but unknown → log a warning and fall back to
      `default`. Boot continues so a typo in env vars does not kill prod.
    - `default` missing or pointing at a missing option → raises.
    """
    orch = raw_models.get("orchestrator")
    if not isinstance(orch, dict) or "provider" in orch:
        return raw_models

    parsed = _split_orchestrator_block(orch)
    chosen = parsed.default
    if agent_model is not None:
        if agent_model in parsed.options:
            chosen = agent_model
        else:
            logger.warning(
                "AGENT_MODEL=%r not in orchestrator options %s; "
                "falling back to default %r",
                agent_model,
                sorted(parsed.options),
                parsed.default,
            )
    raw_models["orchestrator"] = parsed.options[chosen].model_dump()
    return raw_models


class ConfidenceWeights(BaseModel):
    base_scores: dict[str, float]
    places_modifiers: dict[str, float]
    multi_source_bonus: float = 0.10
    max_score: float = 0.95


class ConfidenceConfig(BaseModel):
    """Per-level confidence scoring config (ADR-029, ADR-057).

    base_scores keys are ExtractionLevel.value strings (e.g. "emoji_regex").
    max_score caps the output — no extraction path earns 1.0.

    Two-band save gate (ADR-057):
      confidence <  save_threshold      → not written, surfaces as "failed".
      save_threshold ≤ c < confident    → written with status "needs_review".
      confidence ≥  confident_threshold → written silently as "saved".
    """

    base_scores: dict[str, float] = {
        "emoji_regex": 0.95,
        "llm_ner": 0.60,
        "subtitle_check": 0.75,
        "whisper_audio": 0.65,
        "vision_frames": 0.55,
        # User-curated lists are explicit saves — treat them as ground truth.
        "google_maps_list": 0.95,
    }
    signal_scores: dict[str, float] = {
        "emoji_marker": 0.92,
        "location_tag": 0.85,
        "caption": 0.75,
        "hashtag": 0.55,
    }
    corroboration_bonus: float = 0.10
    max_score: float = 0.97
    save_threshold: float = 0.30
    confident_threshold: float = 0.70


class ExtractionThresholds(BaseModel):
    store_silently: float = 0.70
    require_confirmation: float = 0.30


class ExtractionVisionConfig(BaseModel):
    max_frames: int = 5
    scene_threshold: float = 0.3
    timeout_seconds: float = 10.0


class ExtractionWhisperConfig(BaseModel):
    timeout_seconds: float = 8.0
    audio_format: str = "opus"
    audio_quality: str = "32k"


class ExtractionSubtitleConfig(BaseModel):
    output_dir: str = "/tmp/subtitles"
    format: str = "vtt"


class ExtractionConfig(BaseModel):
    confidence_weights: ConfidenceWeights
    thresholds: ExtractionThresholds
    mutable_fields: list[str] = [
        "place_name",
        "address",
        "cuisine",
        "price_range",
        "lat",
        "lng",
        "source_url",
        "validated_at",
        "confidence",
        "source",
    ]
    confidence: ConfidenceConfig = ConfidenceConfig()
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown: float = 900.0
    vision: ExtractionVisionConfig = ExtractionVisionConfig()
    whisper: ExtractionWhisperConfig = ExtractionWhisperConfig()
    subtitle: ExtractionSubtitleConfig = ExtractionSubtitleConfig()


class ExternalServiceConfig(BaseModel):
    base_url: str
    timeout_seconds: float


class GooglePlacesConfig(ExternalServiceConfig):
    nearbysearch_url: str = (
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    )
    request_fields: list[str] = ["name", "formatted_address", "place_id", "geometry"]
    default_region: str = "th"


class ExternalServicesConfig(BaseModel):
    google_places: GooglePlacesConfig = GooglePlacesConfig(
        base_url="https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
        timeout_seconds=5.0,
    )
    tiktok_oembed: ExternalServiceConfig = ExternalServiceConfig(
        base_url="https://www.tiktok.com/oembed", timeout_seconds=3.0
    )


class EmbeddingsConfig(BaseModel):
    """Embedding configuration (ADR-054).

    `description_fields` drives the order and inclusion of `PlaceObject`
    Tier 1 fields in the embedding input. The persistence layer walks this
    list and emits each available value separated by
    `description_separator`. Retrieval evals can re-tune field order and
    inclusion by editing the config and re-embedding — no code change.
    """

    dimensions: int = 1024
    description_separator: str = " | "
    description_fields: list[str] = [
        "place_name",
        "subcategory",
        "place_type",
        "cuisine",
        "ambiance",
        "price_hint",
        "tags",
        "good_for",
        "dietary",
        "neighborhood",
        "city",
        "country",
    ]


class SystemPromptsConfig(BaseModel):
    consult: str = (
        "You are Totoro, an AI place recommendation assistant. "
        "Answer the user's query helpfully and concisely."
    )


class ConsultConfig(BaseModel):
    max_alternatives: int = 2
    placeholder_photo_url: str = "https://placehold.co/800x450.webp"
    response_timeout_seconds: int = 10
    default_radius_m: int = 1500
    nearby_radius_m: int = 500
    walking_radius_m: int = 1000
    named_location_radius_m: int = 10000


class RecallConfig(BaseModel):
    max_results: int = 10
    rrf_k: int = 60
    candidate_multiplier: int = 2
    min_rrf_score: float = 0.01
    max_cosine_distance: float = 0.65


class TasteRegenConfig(BaseModel):
    """Regen thresholds for taste profile regeneration."""

    min_signals: int = 3
    early_signal_threshold: int = 10


class WarmingBlendConfig(BaseModel):
    """Warming-tier candidate-count ratio (feature 023).

    Applied in `ConsultService.consult` when the user's signal_tier is
    "warming". Values must sum to 1.0 — enforced below.
    """

    discovered: float = 0.8
    saved: float = 0.2

    @model_validator(mode="after")
    def _sum_to_one(self) -> "WarmingBlendConfig":
        if abs((self.discovered + self.saved) - 1.0) > 1e-6:
            raise ValueError(
                f"warming_blend weights must sum to 1.0 "
                f"(got discovered={self.discovered}, saved={self.saved})"
            )
        return self


class TasteModelConfig(BaseModel):
    """Taste model configuration (ADR-058: signal_counts + LLM summary + chips)."""

    debounce_window_seconds: int = 30
    regen: TasteRegenConfig = TasteRegenConfig()
    chip_threshold: int = 2
    chip_max_count: int = 8
    chip_selection_stages: dict[str, int] = Field(
        default_factory=lambda: {"round_1": 5, "round_2": 20, "round_3": 50}
    )
    warming_blend: WarmingBlendConfig = WarmingBlendConfig()


class MemoryConfidenceConfig(BaseModel):
    """Personal fact confidence thresholds."""

    stated: float = 0.9
    inferred: float = 0.6


class MemoryExtractionConfig(BaseModel):
    """Personal-fact extraction batching."""

    debounce_messages: int = 5
    buffer_ttl_seconds: int = 604800


class MemoryConfig(BaseModel):
    """User memory layer configuration."""

    confidence: MemoryConfidenceConfig = MemoryConfidenceConfig()
    extraction: MemoryExtractionConfig = MemoryExtractionConfig()


class ProviderEndpointConfig(BaseModel):
    """Non-secret provider config (base URL, etc.). API keys live in EnvConfig."""

    base_url: str


class AppProvidersConfig(BaseModel):
    """Non-secret provider endpoints (base URLs). API keys live in EnvConfig."""

    groq: ProviderEndpointConfig = ProviderEndpointConfig(
        base_url="https://api.groq.com"
    )
    ollama: ProviderEndpointConfig = ProviderEndpointConfig(
        base_url="http://localhost:11434/v1"
    )


class PlacesConfig(BaseModel):
    """PlacesService cache TTL and per-request fetch cap (ADR-054, feature 019).

    - cache_ttl_days: single lifetime for both the Tier 2 geo cache and the
      Tier 3 enrichment cache. Both set_batch methods in PlacesCache use
      `cache_ttl_days * 86400` seconds. Default 30 days.
    - max_enrichment_batch: per-request cap on external provider fetches
      in PlacesService.enrich_batch(geo_only=False). Counts unique provider_id,
      not input positions.
    """

    cache_ttl_days: int = 30
    max_enrichment_batch: int = 10


class PromptConfig(BaseModel):
    """A loaded prompt template (ADR-059).

    YAML declares `name: filename`. On config load, the file is read
    and the content is stored here. Access via get_config().prompts["name"].content.
    """

    name: str
    file: str
    content: str


class ToolTimeoutsConfig(BaseModel):
    """Per-tool asyncio.wait_for budgets in seconds (feature 027 M2, M9).

    Consumed by the agent tool wrappers (M5) and the timeout guard (M9).
    Not read in this feature — presence + type is the only requirement.
    """

    recall: int = 5
    consult: int = 10
    save: int = 60  # accommodates Apify-backed Google Maps list scrapes

    @model_validator(mode="after")
    def _positive_integers(self) -> "ToolTimeoutsConfig":
        if self.recall < 1 or self.consult < 1 or self.save < 1:
            raise ValueError(
                "agent.tool_timeouts_seconds fields must be >= 1 "
                f"(got recall={self.recall}, consult={self.consult}, save={self.save})"
            )
        return self


class AgentConfig(BaseModel):
    """Typed configuration for the agent path (feature 027 M2, ADR-062).

    `max_steps` and `max_errors` bound the graph's should_continue loop (M3 reads these).
    `checkpointer_ttl_seconds` is reserved for a future cleanup job
    (Postgres has no native TTL).
    `prompt_caching_enabled` wraps the system message in an Anthropic
    `cache_control: ephemeral` block (ADR-067). Disable for non-Anthropic orchestrators.
    """

    max_steps: int = 10
    max_errors: int = 3
    max_history_messages: int = 40
    checkpointer_ttl_seconds: int = 86400
    tool_timeouts_seconds: ToolTimeoutsConfig = ToolTimeoutsConfig()
    prompt_caching_enabled: bool = True

    @model_validator(mode="after")
    def _positive_integers(self) -> "AgentConfig":
        if (
            self.max_steps < 1
            or self.max_errors < 1
            or self.max_history_messages < 1
            or self.checkpointer_ttl_seconds < 1
        ):
            raise ValueError(
                "agent.max_steps / max_errors / max_history_messages / "
                "checkpointer_ttl_seconds must be >= 1 "
                f"(got max_steps={self.max_steps}, max_errors={self.max_errors}, "
                f"max_history_messages={self.max_history_messages}, "
                f"checkpointer_ttl_seconds={self.checkpointer_ttl_seconds})"
            )
        return self


class AppConfig(BaseModel):
    app: AppMeta
    models: dict[str, LLMRoleConfig]
    extraction: ExtractionConfig
    providers: AppProvidersConfig = AppProvidersConfig()
    external_services: ExternalServicesConfig = ExternalServicesConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    system_prompts: SystemPromptsConfig = SystemPromptsConfig()
    consult: ConsultConfig = ConsultConfig()
    recall: RecallConfig = RecallConfig()
    taste_model: TasteModelConfig = TasteModelConfig()
    memory: MemoryConfig = MemoryConfig()
    places: PlacesConfig = PlacesConfig()
    agent: AgentConfig = AgentConfig()
    prompts: dict[str, PromptConfig] = {}

    @model_validator(mode="before")
    @classmethod
    def _resolve_orchestrator_default(cls, data: Any) -> Any:
        """Default-only orchestrator resolution (ADR-068).

        The env-aware path runs in `get_config()` and replaces the
        orchestrator block with a flat dict before AppConfig sees it.
        Direct `AppConfig(**raw)` calls (e.g. from tests) hit this
        validator instead and resolve to the `default` option.
        """
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
            _resolve_orchestrator(data["models"], agent_model=None)
        return data


# Per-prompt required template-slot registry (feature 027 FR-018a).
# Eager validation at _load_prompts() ensures any missing slot aborts boot.
_REQUIRED_PROMPT_SLOTS: dict[str, list[str]] = {
    "agent": ["{taste_profile_summary}", "{memory_summary}"],
}


def _load_prompts(raw: dict[str, str]) -> dict[str, PromptConfig]:
    """Read prompt files from disk and return loaded PromptConfig objects.

    Validates that each prompt contains all required template slots for
    its logical name (feature 027 FR-018a). Missing slot aborts boot
    with a clear error.
    """
    prompts_dir = find_project_root() / "config" / "prompts"
    loaded: dict[str, PromptConfig] = {}
    for name, filename in raw.items():
        path = prompts_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt '{name}' file not found: {path}")
        content = path.read_text()
        for slot in _REQUIRED_PROMPT_SLOTS.get(name, []):
            if slot not in content:
                raise ValueError(
                    f"Prompt {name!r} ({path}) is missing required "
                    f"template slot {slot!r}"
                )
        loaded[name] = PromptConfig(
            name=name,
            file=filename,
            content=content,
        )
    return loaded


_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Return the AppConfig singleton, loading app.yaml on first call.

    Prompt files are read from disk during this call (ADR-059).
    The orchestrator block is resolved against `AGENT_MODEL` here (ADR-068)
    before AppConfig sees `models["orchestrator"]`.
    """
    global _config
    if _config is None:
        raw = load_yaml_config("app.yaml")
        raw["models"] = _resolve_orchestrator(
            raw.get("models") or {}, agent_model=get_env().AGENT_MODEL
        )
        raw["prompts"] = _load_prompts(raw.get("prompts") or {})
        _config = AppConfig(**raw)
    return _config


def get_prompt(name: str) -> str:
    """Get a loaded prompt's content by logical name (ADR-059).

    Raises:
        KeyError: If name not found in app.yaml prompts section.
    """
    config = get_config()
    prompt = config.prompts.get(name)
    if prompt is None:
        raise KeyError(
            f"Prompt '{name}' not found in app.yaml prompts section. "
            f"Available: {list(config.prompts.keys())}"
        )
    return prompt.content


# ---------------------------------------------------------------------------
# EnvConfig — env vars and secrets from .env or environment variables
# ---------------------------------------------------------------------------

_ENV_FILE = find_project_root() / ".env"


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str
    REDIS_URL: str = ""
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    VOYAGE_API_KEY: str | None = None
    GOOGLE_API_KEY: str | None = None
    GROQ_API_KEY: str | None = None
    APIFY_TOKEN: str | None = None
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str | None = None
    AGENT_ENABLED: bool = True
    AGENT_MODEL: str | None = None  # ADR-068: orchestrator option key override


_env: EnvConfig | None = None


def get_env() -> EnvConfig:
    """Return the EnvConfig singleton.

    Reads from .env (local dev) and environment variables (Railway).
    Environment variables take precedence over .env values.
    """
    global _env
    if _env is None:
        _env = EnvConfig()
    return _env
