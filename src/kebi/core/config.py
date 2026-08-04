"""Central config module — single source of truth for all app configuration.

Two singletons:
- get_config()   → AppConfig    from config/app.yaml (committed, non-secret)
- get_env()  → EnvConfig from .env → env vars (never committed)

All other modules import from here. Nobody calls load_yaml_config() directly.
"""

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kebi.core.agent.location import MovementMode, Reach

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


# Selector keys inside the orchestrator block — every other key is a model
# option. `default` picks the standard-tier orchestrator; `advanced`
# (optional) names the option the top plan tier gets, exposed as the
# separate `orchestrator_advanced` role so it survives boot resolution
# instead of being collapsed away with the other options.
_ORCH_RESERVED_KEYS = frozenset({"default", "advanced"})


def _split_orchestrator_block(raw_orch: dict[str, Any]) -> OrchestratorOptionsConfig:
    """Parse the YAML orchestrator block into OrchestratorOptionsConfig.

    `default` and `advanced` are reserved selector keys; every other key is
    an option name mapping to an `LLMRoleConfig`-shaped dict.
    """
    if "default" not in raw_orch:
        raise ValueError(
            "models.orchestrator must define a 'default' key naming one of "
            "its option keys"
        )
    default = raw_orch["default"]
    options = {k: v for k, v in raw_orch.items() if k not in _ORCH_RESERVED_KEYS}
    return OrchestratorOptionsConfig(default=default, options=options)


def _resolve_orchestrator(
    raw_models: dict[str, Any], agent_model: str | None
) -> dict[str, Any]:
    """Resolve a `{default, <option>, ...}` orchestrator block to a flat
    `LLMRoleConfig` dict (ADR-068).

    No-op if the block is already flat (i.e. has top-level `provider`/`model`).
    Mutates `raw_models` in place and returns it.

    - `agent_model` is None  → use `default`.
    - `agent_model` matches an option key → use that option.
    - `agent_model` is set but unknown → log a warning and fall back to
      `default`. Boot continues so a typo in env vars does not kill prod.
    - `default` missing or pointing at a missing option → raises.

    When the block names an `advanced` option, that option is emitted as the
    separate `orchestrator_advanced` role (selected per request for the
    `advanced_models_enabled` plan tier). It references one of the existing
    options — no duplicate model definition. A missing `advanced` key just
    means the role is not defined (the agent path falls back to standard).
    """
    orch = raw_models.get("orchestrator")
    if not isinstance(orch, dict) or "provider" in orch:
        return raw_models

    advanced_key = orch.get("advanced")
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
    if advanced_key is not None:
        if advanced_key not in parsed.options:
            raise ValueError(
                f"orchestrator.advanced={advanced_key!r} not found in options "
                f"{sorted(parsed.options)}"
            )
        raw_models["orchestrator_advanced"] = parsed.options[advanced_key].model_dump()
    return raw_models


class ConfidenceWeights(BaseModel):
    base_scores: dict[str, float]
    places_modifiers: dict[str, float]
    multi_source_bonus: float = 0.10
    max_score: float = 0.95


class ConfidenceConfig(BaseModel):
    """Per-level confidence scoring config (ADR-029, ADR-057).

    `producer_scores` keys are `Producer.value` strings (e.g. "llm_ner").
    `medium_scores` keys are `Medium.value` strings (e.g. "caption").
    `max_score` caps the output — no extraction path earns 1.0.

    Two-band save gate (ADR-057):
      confidence <  save_threshold      → not written, surfaces as "failed".
      save_threshold ≤ c < confident    → written with status "needs_review".
      confidence ≥  confident_threshold → written silently as "saved".
    """

    producer_scores: dict[str, float] = {
        # Name producers
        "llm_ner": 0.60,
        # User-curated lists are explicit saves — treat them as ground truth.
        "google_maps_list": 0.95,
        # Instagram-tagged caption / location-tag — moderate signal.
        "instagram_post": 0.65,
        "vision_frames": 0.55,
        "vision_images": 0.55,
        # Text producers — modest baseline; corroboration bonus when paired
        # with a name producer is what actually lifts confidence.
        "tiktok_caption": 0.65,
        "video_metadata": 0.60,
        "whisper_audio": 0.65,
        "subtitle_check": 0.75,
        "photo_detector": 0.50,
    }
    medium_scores: dict[str, float] = {
        "emoji_marker": 0.92,
        "location_tag": 0.85,
        "caption": 0.75,
        "title": 0.70,
        "transcript": 0.65,
        "supplementary_text": 0.70,
        "hashtag": 0.55,
        "frame": 0.55,
        "image": 0.55,
        "list": 0.95,
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
        "source_ref",
        "validated_at",
        "confidence",
        "source",
    ]
    confidence: ConfidenceConfig = ConfidenceConfig()
    circuit_breaker_threshold: int = 3
    circuit_breaker_cooldown: float = 900.0
    # ADR-074: TTL for the URL-keyed extraction result cache. 30 days
    # — long enough to capture viral spread of a TikTok / Instagram /
    # YouTube share; short enough that edited or deleted content
    # washes out within a month.
    result_cache_ttl_seconds: int = 30 * 24 * 60 * 60
    vision: ExtractionVisionConfig = ExtractionVisionConfig()
    whisper: ExtractionWhisperConfig = ExtractionWhisperConfig()
    subtitle: ExtractionSubtitleConfig = ExtractionSubtitleConfig()


class ExternalServiceConfig(BaseModel):
    base_url: str
    timeout_seconds: float


class ExternalServicesConfig(BaseModel):
    tiktok_oembed: ExternalServiceConfig = ExternalServiceConfig(
        base_url="https://www.tiktok.com/oembed", timeout_seconds=3.0
    )


class GeocodingReverseCacheConfig(BaseModel):
    """Redis cache in front of reverse geocoding.

    `precision` is the coordinate rounding in decimal places (3 ≈ 110 m
    buckets — city/neighborhood granularity). `ttl_seconds` is capped at
    30 days in code to honor the provider's result-caching terms.
    """

    ttl_seconds: int = 30 * 24 * 60 * 60
    precision: int = 3


class AreasConfig(BaseModel):
    """Area layer (location-kinds Step 2). `noted_resolution_limit` caps
    how many noted non-venue names one share may resolve through the
    geocoder — bounds fan-out and spend per harvest."""

    noted_resolution_limit: int = 5


class GeocodingConfig(BaseModel):
    """Geocoding boundary (area layer). `provider` names the adapter —
    swapping providers is a config + adapter change, never a call-site
    change."""

    provider: str = "google"
    timeout_seconds: float = 10.0
    reverse_cache: GeocodingReverseCacheConfig = GeocodingReverseCacheConfig()


class EmbeddingsConfig(BaseModel):
    """Embedding configuration (ADR-054).

    `description_fields` drives the order and inclusion of `PlaceObject`
    Tier 1 fields in the embedding input. The persistence layer walks this
    list and emits each available value separated by
    `description_separator`. Retrieval evals can re-tune field order and
    inclusion by editing the config and re-embedding — no code change.

    `hard_timeout_seconds` / `rate_limit_cooldown_seconds` tune the
    process-wide circuit breaker in `providers/embeddings.VoyageEmbedder`:
    the SDK's internal `tenacity` retry chain takes ~15s to exhaust on
    rate-limit responses, so the breaker hard-times-out individual calls
    and short-circuits subsequent ones for `rate_limit_cooldown_seconds`.
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
    # Voyage rate-limit circuit breaker tuning.
    hard_timeout_seconds: float = 3.0
    rate_limit_cooldown_seconds: float = 60.0


class SystemPromptsConfig(BaseModel):
    consult: str = (
        "You are Kebi, an AI place recommendation assistant. "
        "Answer the user's query helpfully and concisely."
    )


class TasteRegenConfig(BaseModel):
    """Regen thresholds for taste profile regeneration."""

    min_signals: int = 3
    early_signal_threshold: int = 10


class SignalWeightsConfig(BaseModel):
    """Per-signal evidence weight in taste aggregation (the conviction ladder).

    A weight is how many units of taste evidence a signal contributes to the
    category/tag/location tree. A passive link-share `save` is worth nothing on
    its own (`save: 0`); a `saved_recommendation` carries a base weight; the
    Library pills `visited` and `liked` add graduated bonuses on top of a saved
    place's base, while `liked_negative` weights a disliked place into the
    rejected branch. Defaults order the ladder visited+liked ≫ liked > saved_
    recommendation > accepted.
    """

    save: int = 0
    accepted: int = 1
    saved_recommendation: int = 2
    visited: int = 2
    liked: int = 3
    liked_negative: int = 3
    rejected: int = 1
    # Location-kinds Step 3 direct-interest signals. A deliberate share of an
    # area/route is louder than a passive link-share `save` (0) but quieter
    # than a visited+liked venue. A 0 here silences the bucket entirely.
    area_interest: int = 2
    experience: int = 2

    def as_mapping(self) -> dict[str, int]:
        """Flat lever→weight map passed to aggregate_signal_counts."""
        return self.model_dump()


class TasteModelConfig(BaseModel):
    """Taste model configuration (ADR-058: signal_counts + LLM summary)."""

    debounce_window_seconds: int = 30
    regen: TasteRegenConfig = TasteRegenConfig()
    signal_weights: SignalWeightsConfig = SignalWeightsConfig()


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


class HomeConfig(BaseModel):
    """Home screen greeting + chips configuration (ADR-111).

    `cache_ttl_seconds` bounds how long a generated greeting+chips payload
    lives in Redis; the cache key's daypart segment guarantees a stale
    "good morning" can never serve into the evening regardless of TTL.
    `chip_min`/`chip_max` bound the generated chip list and the static
    fallback emits exactly `chip_min` chips.
    """

    cache_ttl_seconds: int = 3600
    chip_min: int = 3
    chip_max: int = 4

    @model_validator(mode="after")
    def _bounds(self) -> "HomeConfig":
        if self.cache_ttl_seconds < 1:
            raise ValueError(
                f"home.cache_ttl_seconds must be >= 1 (got {self.cache_ttl_seconds})"
            )
        if self.chip_min < 1 or self.chip_max < 1:
            raise ValueError(
                "home.chip_min / chip_max must be >= 1 "
                f"(got chip_min={self.chip_min}, chip_max={self.chip_max})"
            )
        if self.chip_min > self.chip_max:
            raise ValueError(
                "home.chip_min must be <= chip_max "
                f"(got chip_min={self.chip_min}, chip_max={self.chip_max})"
            )
        return self


class KnowledgeResearchConfig(BaseModel):
    """Research read-path knobs (the knowledge layer's agent-facing reader).

    `entity_confidence_min` is the resolve-vs-clarify threshold: a resolved
    entity below it clarifies instead of retrieving (staged resolver emits
    1.0 for an exact working-location match, 0.8 for a verified geocode).
    The `w_*` weights shape the in-memory Stage-C rank (tag match on the
    controlled vocabulary, lexical text overlap, writer trust, proximity to
    the asked scope). `topic_relevance_floor` is the `no_topic_match` cutoff
    on the relevance component — applied only when the question carries
    topic signal, so a broad "tell me about X" never trips it.
    """

    entity_confidence_min: float = 0.5
    w_tag: float = 0.5
    w_text: float = 0.3
    w_trust: float = 0.2
    w_prox: float = 0.1
    topic_relevance_floor: float = 0.05

    @model_validator(mode="after")
    def _bounds(self) -> "KnowledgeResearchConfig":
        for name, value in (
            ("entity_confidence_min", self.entity_confidence_min),
            ("topic_relevance_floor", self.topic_relevance_floor),
        ):
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"knowledge.research.{name} must be in [0.0, 1.0] (got {value})"
                )
        for name, value in (
            ("w_tag", self.w_tag),
            ("w_text", self.w_text),
            ("w_trust", self.w_trust),
            ("w_prox", self.w_prox),
        ):
            if value < 0.0:
                raise ValueError(
                    f"knowledge.research.{name} must be >= 0 (got {value})"
                )
        return self


class KnowledgeConfig(BaseModel):
    """Knowledge-layer writer settings (ADR-120/121/122).

    Confidence floors encode source trust: a harvested claim (one mention in
    one share) floors low; a curated-expert claim floors high. The writer
    takes `max(floor, model_estimate)`, capped at 1.0, so an obvious fact can
    still score above its floor but a weak source can never masquerade as
    strong.

    The `*_review_status` fields are the review gate (ADR-122): the state a
    fresh claim from each source lands in. Both default `approved` — the
    product trusts every writer today. Turning on review (e.g. setting
    `harvest_review_status: pending`) is this config change, not code.
    """

    harvest_confidence_floor: float = 0.35
    curator_confidence_floor: float = 0.9
    # The saved-recommendation reason, written as a user-scoped `kebi_message`
    # claim on save (ADR-127). Floored between harvested (weak) and curated
    # (strong): it is the user's own rationale, trusted for them but not global
    # expertise. `place_notes_limit` caps how many notes surface on one place.
    kebi_message_confidence_floor: float = 0.8
    harvest_review_status: Literal["pending", "approved", "rejected"] = "approved"
    curator_review_status: Literal["pending", "approved", "rejected"] = "approved"
    kebi_message_review_status: Literal["pending", "approved", "rejected"] = "approved"
    place_notes_limit: int = 6
    research: KnowledgeResearchConfig = KnowledgeResearchConfig()

    @model_validator(mode="after")
    def _bounds(self) -> "KnowledgeConfig":
        for name, value in (
            ("harvest_confidence_floor", self.harvest_confidence_floor),
            ("curator_confidence_floor", self.curator_confidence_floor),
            ("kebi_message_confidence_floor", self.kebi_message_confidence_floor),
        ):
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"knowledge.{name} must be in [0.0, 1.0] (got {value})"
                )
        if self.place_notes_limit < 1:
            raise ValueError(
                "knowledge.place_notes_limit must be >= 1 "
                f"(got {self.place_notes_limit})"
            )
        return self


class UserIntentConfig(BaseModel):
    """Write gates for the "what you wanted" recall list (ADR-110).

    The agent-signal gate (a turn actually surfaced places) is the primary
    filter applied at the call site; these are the cheap heuristic backstop.
    `min_words` rejects terse turns, `stoplist` drops pure confirmations /
    ordinals / pronoun replies, and `dedup_window_seconds` suppresses a new
    intent that duplicates the user's most recent one within the window.
    """

    min_words: int = 3
    stoplist: list[str] = []
    dedup_window_seconds: int = 600

    @model_validator(mode="after")
    def _bounds(self) -> "UserIntentConfig":
        if self.min_words < 1:
            raise ValueError(
                f"user_intents.min_words must be >= 1 (got {self.min_words})"
            )
        if self.dedup_window_seconds < 0:
            raise ValueError(
                "user_intents.dedup_window_seconds must be >= 0 "
                f"(got {self.dedup_window_seconds})"
            )
        return self


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


class PromptConfig(BaseModel):
    """A loaded prompt template (ADR-059).

    YAML declares `name: filename`. On config load, the file is read
    and the content is stored here. Access via get_config().prompts["name"].content.
    """

    name: str
    file: str
    content: str


class ToolTimeoutsConfig(BaseModel):
    """Per-tool asyncio.wait_for budgets in seconds.

    Consumed by the timeout guard in `core/agent/tools/_with_timeout.py`.
    One field per live tool — extended as new consult-family tools land.
    """

    find_saved: int = 8
    suggest_places: int = 18
    discover_places: int = 8
    research: int = 8

    @model_validator(mode="after")
    def _positive_integers(self) -> "ToolTimeoutsConfig":
        if (
            self.find_saved < 1
            or self.suggest_places < 1
            or self.discover_places < 1
            or self.research < 1
        ):
            raise ValueError(
                "agent.tool_timeouts_seconds fields must be >= 1 "
                f"(got find_saved={self.find_saved}, "
                f"suggest_places={self.suggest_places}, "
                f"discover_places={self.discover_places}, "
                f"research={self.research})"
            )
        return self


class FindSavedConfig(BaseModel):
    """Per-tool knobs for `find_saved`.

    `default_limit` is what the tool uses when the agent omits the LLM
    `limit` arg. `max_limit` caps any agent-supplied value so the LLM
    cannot ask for an unbounded result set.
    """

    default_limit: int = 10
    max_limit: int = 25

    @model_validator(mode="after")
    def _positive_integers(self) -> "FindSavedConfig":
        if self.default_limit < 1 or self.max_limit < 1:
            raise ValueError(
                "agent.find_saved.default_limit / max_limit must be >= 1 "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.find_saved.default_limit must be <= max_limit "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        return self


class SuggestPlacesConfig(BaseModel):
    """Per-tool knobs for `suggest_places`.

    `default_limit` / `max_limit` mirror `FindSavedConfig` — agent-facing
    caps on returned candidates. `name_count` is how many candidate names
    the namer LLM is asked to produce per call — kept higher than the
    typical limit so the provider-validation + constraint-filter steps
    have headroom to drop misses. `provider_concurrency` bounds the
    fan-out into `PlacesSearchService.find()` so a noisy namer can't
    overwhelm Google's quota.
    """

    default_limit: int = 5
    max_limit: int = 15
    name_count: int = 8
    provider_concurrency: int = 5

    @model_validator(mode="after")
    def _positive_integers(self) -> "SuggestPlacesConfig":
        if (
            self.default_limit < 1
            or self.max_limit < 1
            or self.name_count < 1
            or self.provider_concurrency < 1
        ):
            raise ValueError(
                "agent.suggest_places fields must be >= 1 "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit}, name_count={self.name_count}, "
                f"provider_concurrency={self.provider_concurrency})"
            )
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.suggest_places.default_limit must be <= max_limit "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        if self.name_count < self.default_limit:
            raise ValueError(
                "agent.suggest_places.name_count must be >= default_limit "
                "(namer must produce enough candidates to survive provider "
                "misses and constraint filtering) "
                f"(got name_count={self.name_count}, "
                f"default_limit={self.default_limit})"
            )
        return self


class DiscoverPlacesConfig(BaseModel):
    """Per-tool knobs for `discover_places`.

    `default_limit` / `max_limit` mirror the other consult-family tools.
    No `name_count` / `provider_concurrency` — the tool issues exactly
    one `PlacesSearchService.find()` call (no fan-out, no namer).
    """

    default_limit: int = 10
    max_limit: int = 25

    @model_validator(mode="after")
    def _positive_integers(self) -> "DiscoverPlacesConfig":
        if self.default_limit < 1 or self.max_limit < 1:
            raise ValueError(
                "agent.discover_places.default_limit / max_limit must be >= 1 "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.discover_places.default_limit must be <= max_limit "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        return self


class ResearchToolConfig(BaseModel):
    """Per-tool knobs for `research`.

    `default_limit` / `max_limit` mirror the other consult-family tools —
    caps on the agent-supplied `limit` arg. `notes_limit` is the service's
    own hard cap on notes returned per call, whatever the agent asked for.
    """

    default_limit: int = 6
    max_limit: int = 10
    notes_limit: int = 10

    @model_validator(mode="after")
    def _positive_integers(self) -> "ResearchToolConfig":
        if self.default_limit < 1 or self.max_limit < 1 or self.notes_limit < 1:
            raise ValueError(
                "agent.research.default_limit / max_limit / notes_limit must "
                f"be >= 1 (got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit}, notes_limit={self.notes_limit})"
            )
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.research.default_limit must be <= max_limit "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        return self


class AgentConfig(BaseModel):
    """Typed configuration for the agent path (feature 027 M2, ADR-062).

    `max_steps` and `max_errors` bound the graph's should_continue loop
    (M3 reads these). `checkpointer_ttl_seconds` is reserved for a future
    cleanup job (Postgres has no native TTL).
    `prompt_caching_enabled` wraps the system message in an Anthropic
    `cache_control: ephemeral` block (ADR-067). Disable for non-Anthropic orchestrators.
    """

    max_steps: int = 10
    max_errors: int = 3
    max_tool_calls: int = 5
    max_history_messages: int = 40
    tool_result_window: int = 2
    state_message_cap: int = 200
    state_message_floor: int = 150
    checkpointer_ttl_seconds: int = 86400
    tool_timeouts_seconds: ToolTimeoutsConfig = ToolTimeoutsConfig()
    find_saved: FindSavedConfig = FindSavedConfig()
    suggest_places: SuggestPlacesConfig = SuggestPlacesConfig()
    discover_places: DiscoverPlacesConfig = DiscoverPlacesConfig()
    research: ResearchToolConfig = ResearchToolConfig()
    prompt_caching_enabled: bool = True

    @model_validator(mode="after")
    def _positive_integers(self) -> "AgentConfig":
        if (
            self.max_steps < 1
            or self.max_errors < 1
            or self.max_tool_calls < 1
            or self.max_history_messages < 1
            or self.checkpointer_ttl_seconds < 1
        ):
            raise ValueError(
                "agent.max_steps / max_errors / max_tool_calls / "
                "max_history_messages / checkpointer_ttl_seconds must be >= 1 "
                f"(got max_steps={self.max_steps}, max_errors={self.max_errors}, "
                f"max_tool_calls={self.max_tool_calls}, "
                f"max_history_messages={self.max_history_messages}, "
                f"checkpointer_ttl_seconds={self.checkpointer_ttl_seconds})"
            )
        if self.max_tool_calls > self.max_steps:
            raise ValueError(
                "agent.max_tool_calls must be <= max_steps "
                "(the LLM-round ceiling must accommodate the tool budget) "
                f"(got max_tool_calls={self.max_tool_calls}, "
                f"max_steps={self.max_steps})"
            )
        if self.tool_result_window < 0:
            raise ValueError(
                f"agent.tool_result_window must be >= 0 (got {self.tool_result_window})"
            )
        if self.state_message_floor < 1 or self.state_message_cap < 1:
            raise ValueError(
                "agent.state_message_cap / state_message_floor must be >= 1 "
                f"(got cap={self.state_message_cap}, "
                f"floor={self.state_message_floor})"
            )
        if self.state_message_floor >= self.state_message_cap:
            raise ValueError(
                "agent.state_message_floor must be < state_message_cap "
                f"(got floor={self.state_message_floor}, "
                f"cap={self.state_message_cap})"
            )
        if self.state_message_floor < self.max_history_messages:
            raise ValueError(
                "agent.state_message_floor must be >= max_history_messages "
                "(otherwise the LLM window would routinely exceed available state) "
                f"(got floor={self.state_message_floor}, "
                f"max_history_messages={self.max_history_messages})"
            )
        return self


class MovementRadiusTiers(BaseModel):
    """Base search radius in metres per scope tier, before mode/reach scaling."""

    walkable: float = 1000.0
    neighborhood: float = 2500.0
    city: float = 7000.0
    metro: float = 45000.0


class MovementFallback(BaseModel):
    """Neutral mobility capability applied when a `/v1/chat` request omits
    `movement_profile` (ADR-085 / ADR-086).

    Deliberately conservative: walking is listed first so the system's
    deterministic mode pick — `available_modes[0]` when the resolver leaves
    `effective_mode` empty — is walking rather than something that silently
    widens every search radius. The resolver may still pick transit per turn
    based on the working location and the message.
    """

    reach: Reach = "normal"
    available_modes: list[MovementMode] = ["walking", "transit"]


class CorridorConfig(BaseModel):
    """Route-shaped search geometry (ADR-136).

    A corridor turn samples points along the route and searches around each.
    `max_waypoints` caps the billed fan-out per turn, but never drops a stop
    the user named — it bounds the *interior* sampling. `max_venue_route_m`
    is the length gate: past it, nothing is meaningfully "on the way" and
    venue stops stop being an honest answer (areas become the right answer,
    which consult cannot return until the roadmap's Step 6).

    The half-width — how far off the route a place still counts — scales with
    route length rather than being flat. The route is a straight chord and a
    road is not, so the approximation error grows with distance: on the 84 km
    Da Nang → Hue coastal drive the real road bows ~16 km off the chord, and
    Lang Co (the canonical stop) sits out there. A flat tolerance either drops
    the obvious stops on a long route or drags in half a city on a short one.
    `half_width_ratio` sets the slope; the floor and ceiling bound it.
    """

    waypoint_spacing_m: float = 25_000.0
    min_waypoints: int = 2
    max_waypoints: int = 5
    half_width_ratio: float = 0.25
    min_half_width_m: float = 5_000.0
    max_half_width_m: float = 25_000.0
    max_stops: int = 5
    max_venue_route_m: float = 300_000.0
    # The enclosing circle a saved-place search fences by is coarse, so a route
    # turn reads wider than it returns and lets the exact route test narrow it.
    # Without it a handful of off-route saves crowd out the ones actually on
    # the way. Saves are a small local pool — the wider read is one DB query.
    saved_overfetch: int = 4


class MovementConfig(BaseModel):
    """Movement / search-scope configuration (ADR-084).

    `radius_tiers` × `mode_multiplier` produce the per-turn search radius;
    `reach` shifts the tier first — see `core/agent/location.resolve_radius`.
    """

    radius_tiers: MovementRadiusTiers = MovementRadiusTiers()
    mode_multiplier: dict[str, float] = {
        "walking": 1.0,
        "cycling": 1.5,
        "transit": 2.0,
        "rideshare": 2.2,
        "motorbike": 2.4,
        "driving": 2.6,
    }
    # Location-density scaling (ADR-084): "near me" reaches further in a
    # sparse area than a dense one. The class is read from the geocoder's
    # place type — never a static table.
    density_factor: dict[str, float] = {
        "dense": 0.7,
        "medium": 1.0,
        "sparse": 1.6,
    }
    fallback: MovementFallback = MovementFallback()
    corridor: CorridorConfig = CorridorConfig()


class VoyagePricing(BaseModel):
    """Per-1M-token rate for Voyage embeddings (not in Langfuse catalog)."""

    input_per_1m: float

    def cost_for(self, total_tokens: int) -> float:
        return self.input_per_1m * (total_tokens / 1_000_000)


class WhisperPricing(BaseModel):
    """Per-second audio rate for Groq Whisper (not in Langfuse catalog)."""

    per_audio_second: float

    def cost_for(self, duration_seconds: float) -> float:
        return self.per_audio_second * duration_seconds


class GooglePlacesPricing(BaseModel):
    """Per-call USD by endpoint path. SKU tier inferred from field mask;
    today every path is Enterprise (the production `_FIELD_MASK` includes
    Enterprise-tier fields). If we ever drop atmosphere fields back to
    Essentials, swap the numbers — the helper signature stays the same.
    """

    per_endpoint: dict[str, float]

    def cost_for(self, endpoint: str) -> float:
        return self.per_endpoint.get(endpoint, 0.0)


class ApifyActorPricing(BaseModel):
    """Per-result rate for one Apify actor. Multiplied by the
    `x-apify-pagination-total` header at the call site — no follow-up
    HTTP request to the run object.
    """

    per_result: float


class ApifyPricing(BaseModel):
    google_maps_list: ApifyActorPricing
    instagram_post: ApifyActorPricing

    def cost_for(self, actor_key: str, item_count: int) -> float:
        actor: ApifyActorPricing | None = getattr(self, actor_key, None)
        if actor is None:
            return 0.0
        return actor.per_result * item_count


class GoogleGeocodingPricing(BaseModel):
    """Per-call USD for the Google Geocoding API (forward + reverse).

    One flat Essentials-tier rate — the API has no field masks or SKU
    tiers. Defaulted so configs written before the geocoder switch still
    validate.
    """

    per_call: float = 0.005


class ExternalProviderPricing(BaseModel):
    google_places: GooglePlacesPricing
    apify: ApifyPricing
    google_geocoding: GoogleGeocodingPricing = GoogleGeocodingPricing()


class PricingConfig(BaseModel):
    """Provider rates for cost attribution in Langfuse traces.

    LLM completions and embeddings priced by Langfuse's catalog are
    listed under `llm` for human reconciliation only — code never reads
    those values. The fields that ARE read by code: `embeddings`,
    `transcription`, and `external`.
    """

    currency: str = "USD"
    llm: dict[str, dict[str, float]] = {}
    embeddings: dict[str, VoyagePricing] = {}
    transcription: dict[str, WhisperPricing] = {}
    external: ExternalProviderPricing


class AppConfig(BaseModel):
    app: AppMeta
    models: dict[str, LLMRoleConfig]
    extraction: ExtractionConfig
    providers: AppProvidersConfig = AppProvidersConfig()
    external_services: ExternalServicesConfig = ExternalServicesConfig()
    geocoding: GeocodingConfig = GeocodingConfig()
    areas: AreasConfig = AreasConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    system_prompts: SystemPromptsConfig = SystemPromptsConfig()
    taste_model: TasteModelConfig = TasteModelConfig()
    memory: MemoryConfig = MemoryConfig()
    home: HomeConfig = HomeConfig()
    user_intents: UserIntentConfig = UserIntentConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    agent: AgentConfig = AgentConfig()
    movement: MovementConfig = MovementConfig()
    pricing: PricingConfig
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
    "agent": [
        "{location_context}",
        "{movement_context}",
        "{taste_profile_summary}",
        "{memory_summary}",
    ],
    "location_resolver": [
        "{current_message}",
        "{conversation_history}",
        "{user_actual_location}",
        "{previous_working_location}",
        "{distance_from_previous}",
        "{mobility_profile}",
    ],
    "candidate_namer": [
        "{intent}",
        "{location_block}",
        "{mobility_block}",
        "{categories_block}",
        "{hard_constraints_block}",
        "{taste_block}",
        "{count}",
    ],
    "home_suggester": [
        "{taste_block}",
        "{location_block}",
        "{time_block}",
        "{weather_block}",
        "{chip_min}",
        "{chip_max}",
    ],
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
    # Gateway service-to-service auth. The NestJS gateway holds the same
    # secret and forwards it on every call as X-Gateway-Token alongside
    # X-Gateway-User-Id (the verified Clerk subject). kebi never sees
    # Clerk tokens directly; it trusts the gateway iff the shared secret
    # validates. Required at startup — startup fails closed if unset.
    GATEWAY_SHARED_SECRET: str | None = None
    # "production" disables /docs, /redoc, /openapi.json and enables
    # strict CORS. Any other value (default "development") leaves docs
    # exposed and CORS permissive for local tooling.
    ENVIRONMENT: str = "development"
    # Comma-separated list of allowed CORS origins for the protected
    # router. Empty = no cross-origin requests permitted. Gateway calls
    # are server-to-server and do not need CORS; this field is for
    # browser-based dev tooling only.
    CORS_ALLOW_ORIGINS: str = ""
    # When true (default), redact user-content fields (message, intent,
    # transcript, city) from Langfuse traces. Disable only in
    # short-lived debug sessions on dev data — production traces must
    # never carry PII.
    LANGFUSE_SCRUB_INPUT: bool = True
    # S3-compatible object storage (Railway / AWS S3 / R2 / MinIO). All
    # unset = NullObjectStorage (no-op fallback for local dev).
    BUCKET_ENDPOINT_URL: str | None = None
    BUCKET_NAME: str | None = None
    BUCKET_ACCESS_KEY_ID: str | None = None
    BUCKET_SECRET_ACCESS_KEY: str | None = None
    BUCKET_REGION: str = "auto"


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
