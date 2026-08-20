"""Central config module — single source of truth for all app configuration.

Two singletons:
- get_config()   → AppConfig    from config/app.yaml (committed, non-secret)
- get_env()  → EnvConfig from .env → env vars (never committed)

All other modules import from here. Nobody calls load_yaml_config() directly.
"""

import logging
import os
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
    # Retry budget owned by kebi, not the SDK. Counts retries *after* the
    # first attempt (0 = single attempt). SDK-internal retries are disabled
    # at client construction so every real API call is visible to tracing —
    # hidden SDK retries multiplied on top of our loops (up to 9 calls per
    # logical call) and never appeared in Langfuse.
    max_retries: int = 2
    # Per-request timeout passed to the provider SDK. None = SDK default.
    timeout_seconds: float | None = None
    # Reasoning-model dial (GPT-5.6 family: none|low|medium|high|...).
    # Required as "none" for gpt-5.6-luna structured-output calls — the
    # chat-completions API rejects function tools at any other effort.
    # None = omit the parameter (non-reasoning models).
    reasoning_effort: str | None = None
    # Claude Sonnet 5 rejects `temperature` outright ("deprecated for this
    # model", 400) — caught by the orchestrator bakeoff before it broke the
    # advanced tier in prod. False = clients omit the parameter entirely;
    # the role's `temperature` value is then ignored by design.
    supports_temperature: bool = True


# Per-role boot override: KEBI_MODEL_<ROLE> (role name upper-cased) names
# a PROFILE from `model_profiles`, e.g. KEBI_MODEL_EXTRACTOR=gpt4o-strong.
# Rollback = unset the var and restart — no deploy, no config edit
# (ADR-173/179).
_MODEL_OVERRIDE_PREFIX = "KEBI_MODEL_"


def expand_profile(
    entry: dict[str, Any], profiles: dict[str, Any], where: str
) -> dict[str, Any]:
    """Merge a `profile:` reference into a role dict (ADR-176/179).

    `model_profiles.<name>` supplies the base fields (provider, model,
    limits, quirks); the entry's own keys override — a role carries only
    what is genuinely per-role (token ceiling, temperature, a tighter
    timeout). A group of roles referencing one profile moves to a new
    model with a single profile edit.
    """
    if "profile" not in entry:
        return entry
    name = entry["profile"]
    if name not in profiles:
        raise ValueError(
            f"{where}: unknown model profile {name!r}; have {sorted(profiles)}"
        )
    base = profiles[name]
    if not isinstance(base, dict):
        raise ValueError(f"model_profiles.{name} must be a mapping")
    return {**base, **{k: v for k, v in entry.items() if k != "profile"}}


def _model_env_overrides() -> dict[str, str]:
    """Collect KEBI_MODEL_<ROLE> overrides from the process environment.

    Process env only — the same place Railway sets variables. Role names
    are recovered by lower-casing (KEBI_MODEL_LOCATION_RESOLVER →
    location_resolver).
    """
    prefix = _MODEL_OVERRIDE_PREFIX
    return {
        key[len(prefix) :].lower(): value
        for key, value in os.environ.items()
        if key.startswith(prefix) and value
    }


def _resolve_model_options(
    raw_models: dict[str, Any],
    overrides: dict[str, str] | None = None,
    agent_model: str | None = None,
    profiles: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve every role's flat `profile:` reference to a flat
    `LLMRoleConfig` dict (ADR-179, superseding ADR-173's option blocks).

    One shape for every role:

        <role>:
          profile: <model_profiles key>   # which model
          max_tokens / temperature / ...  # per-role params (override profile)

    Switching a role's model is `KEBI_MODEL_<ROLE>=<profile-name>` in the
    env (unknown name → warn + keep the configured profile, so a typo
    never kills prod boot), or editing the role's `profile:` line; moving
    a whole group is editing its shared profile in `model_profiles`.

    Orchestrator extras: `AGENT_MODEL` works as an alias for
    `KEBI_MODEL_ORCHESTRATOR` (which wins when both are set), and an
    `advanced: <profile-name>` key emits the separate
    `orchestrator_advanced` role — same per-role params, advanced-tier
    profile. `advanced` on any other role is an error.

    Blocks that inline `provider` without a `profile` pass through
    untouched (test fixtures). Mutates `raw_models` in place, returns it.
    """
    overrides = overrides or {}
    if profiles is None:
        # Back-compat callers resolve against the committed catalog.
        # Pass `{}` explicitly to mean "no profiles".
        profiles = load_yaml_config("app.yaml").get("model_profiles") or {}
    for role in list(raw_models):
        block = raw_models[role]
        if not isinstance(block, dict):
            continue
        entry = dict(block)
        advanced_profile = entry.pop("advanced", None)
        if advanced_profile is not None and role != "orchestrator":
            raise ValueError(
                f"models.{role}: 'advanced' is reserved for the orchestrator "
                "(plan-tier selector)"
            )
        if "profile" not in entry:
            # Inline provider block (test fixtures) — leave as-is.
            continue
        requested = overrides.get(role)
        override_source = f"{_MODEL_OVERRIDE_PREFIX}{role.upper()}"
        if requested is None and role == "orchestrator" and agent_model is not None:
            requested = agent_model
            override_source = "AGENT_MODEL"
        if requested is not None:
            if requested in profiles:
                logger.info(
                    "models.%s: profile %r selected via %s (configured: %r)",
                    role,
                    requested,
                    override_source,
                    entry["profile"],
                )
                entry["profile"] = requested
            else:
                logger.warning(
                    "%s=%r is not a model profile %s; keeping configured profile %r",
                    override_source,
                    requested,
                    sorted(profiles),
                    entry["profile"],
                )
        raw_models[role] = expand_profile(entry, profiles, f"models.{role}")
        if advanced_profile is not None:
            adv_entry = {
                **{k: v for k, v in block.items() if k != "advanced"},
                "profile": advanced_profile,
            }
            raw_models["orchestrator_advanced"] = expand_profile(
                adv_entry, profiles, "models.orchestrator.advanced"
            )
    return raw_models


def _resolve_orchestrator(
    raw_models: dict[str, Any], agent_model: str | None
) -> dict[str, Any]:
    """Back-compat entry point (ADR-068) — resolves ALL role blocks, with
    `agent_model` as the orchestrator's profile-override alias."""
    return _resolve_model_options(raw_models, agent_model=agent_model)


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


class AreasConfig(BaseModel):
    """Area screen + profiler knobs (ADR-153).

    `claims_input_limit` caps how many approved claims feed one profiling
    call — highest-confidence first when it bites. `notable_sub_areas_max`
    caps the profiler's "worth knowing" children stored on the row.
    """

    claims_input_limit: int = 30
    notable_sub_areas_max: int = 6

    @model_validator(mode="after")
    def _bounds(self) -> "AreasConfig":
        if self.claims_input_limit < 1 or self.notable_sub_areas_max < 1:
            raise ValueError(
                "areas.claims_input_limit / notable_sub_areas_max must be >= 1 "
                f"(got {self.claims_input_limit}, {self.notable_sub_areas_max})"
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


class EntitySearchConfig(BaseModel):
    """The curation anchor-chip typeahead (/v1/knowledge/entities).

    `area_limit` caps how many area rows lead the result list — areas are
    few and exact-ish; the rest of the page is places. The resolver cache
    TTL governs how long a verified-or-miss geocode verdict for an unseen
    area name is remembered; long on purpose, since "is X a real city in
    country Y" barely changes and public Nominatim is rate-limited.
    """

    area_limit: int = 3
    resolver_cache_ttl_seconds: int = 604800  # 7 days


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
    # Web-mined claims (ADR-145) floor lowest of all: a search snippet is one
    # unreviewed page, weaker evidence than something a person cared enough
    # to share. It still surfaces — trust is a ranking weight, not a gate —
    # but it loses to a harvested or curated claim that disagrees.
    web_search_confidence_floor: float = 0.25
    harvest_review_status: Literal["pending", "approved", "rejected"] = "approved"
    web_search_review_status: Literal["pending", "approved", "rejected"] = "approved"
    curator_review_status: Literal["pending", "approved", "rejected"] = "approved"
    kebi_message_review_status: Literal["pending", "approved", "rejected"] = "approved"
    place_notes_limit: int = 6
    # Insider notes attached to a place tool's result on the retrieval path
    # (ADR-137). `candidate_notes_limit` is per candidate and stays small — a
    # recommendation list of 10 places carries 10x this, and the notes are
    # material for one line of prose each, not a dossier. `area_notes_limit`
    # is per turn: neighborhood, city, and country claims pooled together.
    candidate_notes_limit: int = 2
    area_notes_limit: int = 3
    research: KnowledgeResearchConfig = KnowledgeResearchConfig()
    entity_search: EntitySearchConfig = EntitySearchConfig()

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
        for name, limit in (
            ("candidate_notes_limit", self.candidate_notes_limit),
            ("area_notes_limit", self.area_notes_limit),
        ):
            if limit < 0:
                raise ValueError(f"knowledge.{name} must be >= 0 (got {limit})")
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
    brave: ProviderEndpointConfig = ProviderEndpointConfig(
        base_url="https://api.search.brave.com"
    )
    # OpenAI-compatible gateway used for benchmarking candidate models
    # (ADR-173). One key covers Gemini/Qwen/DeepSeek/etc.; production
    # winners get a direct provider integration before promotion.
    openrouter: ProviderEndpointConfig = ProviderEndpointConfig(
        base_url="https://openrouter.ai/api/v1"
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
    research: int = 8
    # find_known is two indexed reads, no LLM and no provider — the cheapest
    # tool in the set (ADR-138).
    find_known: int = 8
    # One outbound HTTP call with its own 6s budget, plus cache lookup. Kept
    # tight: a slow search must lose the nuance, not the turn (ADR-145).
    web_search: int = 10

    @model_validator(mode="after")
    def _positive_integers(self) -> "ToolTimeoutsConfig":
        fields = {
            "find_saved": self.find_saved,
            "suggest_places": self.suggest_places,
            "research": self.research,
            "find_known": self.find_known,
            "web_search": self.web_search,
        }
        bad = {k: v for k, v in fields.items() if v < 1}
        if bad:
            raise ValueError(
                f"agent.tool_timeouts_seconds fields must be >= 1 (got {bad})"
            )
        return self


class FindKnownConfig(BaseModel):
    """Per-tool knobs for `find_known` (ADR-138).

    `scan_limit` bounds the geofenced claims join — the ceiling on how many
    claim rows one call ranks in memory, not how many places come back.
    `notes_per_place` caps the facts carried per surfaced place: they are the
    reason it surfaced, so this runs a little richer than the passive
    `knowledge.candidate_notes_limit`.
    """

    default_limit: int = 5
    max_limit: int = 15
    notes_per_place: int = 3
    scan_limit: int = 300

    @model_validator(mode="after")
    def _positive_integers(self) -> "FindKnownConfig":
        fields = {
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
            "notes_per_place": self.notes_per_place,
            "scan_limit": self.scan_limit,
        }
        bad = {k: v for k, v in fields.items() if v < 1}
        if bad:
            raise ValueError(f"agent.find_known fields must be >= 1 (got {bad})")
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.find_known.default_limit must be <= max_limit "
                f"(got {self.default_limit} > {self.max_limit})"
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


class WebSearchToolConfig(BaseModel):
    """Per-tool knobs for `web_search` (ADR-145).

    `snippet_max_chars` is the important one. Findings compete for the
    orchestrator's attention with the claims, and the claims are the part of
    an answer that is ours — so a finding is capped at roughly a long sentence
    and a half: enough to ground a date or a price, not enough to become the
    answer.

    `cache_ttl_seconds` is one day. The tool fires freely by design, so the
    cache is what keeps that affordable; a day is short enough that a
    schedule change washes out by tomorrow and long enough that a question
    trending across users is paid for once.

    `harvest_enabled` gates the write-back into the claims store. Config, not
    code, so a bad harvest can be switched off without a deploy.
    """

    default_limit: int = 5
    max_limit: int = 8
    snippet_max_chars: int = 320
    cache_ttl_seconds: int = 86400
    harvest_enabled: bool = True

    @model_validator(mode="after")
    def _positive_integers(self) -> "WebSearchToolConfig":
        if self.default_limit < 1 or self.max_limit < 1:
            raise ValueError(
                "agent.web_search.default_limit / max_limit must be >= 1 "
                f"(got default_limit={self.default_limit}, "
                f"max_limit={self.max_limit})"
            )
        if self.default_limit > self.max_limit:
            raise ValueError(
                "agent.web_search.default_limit must be <= max_limit "
                f"(got {self.default_limit} > {self.max_limit})"
            )
        if self.snippet_max_chars < 80:
            raise ValueError(
                "agent.web_search.snippet_max_chars must be >= 80 — below that "
                "a finding is too short to ground a fact on "
                f"(got {self.snippet_max_chars})"
            )
        if self.cache_ttl_seconds < 1:
            raise ValueError(
                "agent.web_search.cache_ttl_seconds must be >= 1 "
                f"(got {self.cache_ttl_seconds})"
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


class ItineraryConfig(BaseModel):
    """Knobs for multi-stop itinerary turns (ADR-148).

    `max_stops` caps how many resolver-named stops the resolve node
    geocodes — a runaway stop list costs one geocode each. `per_segment_limit`
    is the per-stop / per-leg candidate cap the fan-out tools use, replacing
    the single-search limit: an itinerary answer wants a few strong names per
    segment, not one city's worth from each. `max_tool_calls` replaces the
    flat per-turn tool budget on itinerary turns: a three-stop trip
    legitimately spends more calls than a single-city question (retrieval,
    per-stop suggestions, the guard's verification round), and the flat cap
    was observed collapsing trip turns into the cap-hit fallback.
    """

    max_stops: int = 5
    per_segment_limit: int = 4
    max_tool_calls: int = 8

    @model_validator(mode="after")
    def _positive_integers(self) -> "ItineraryConfig":
        if self.max_stops < 2 or self.per_segment_limit < 1:
            raise ValueError(
                "agent.itinerary.max_stops must be >= 2 and "
                "per_segment_limit >= 1 "
                f"(got max_stops={self.max_stops}, "
                f"per_segment_limit={self.per_segment_limit})"
            )
        if self.max_tool_calls < 1:
            raise ValueError(
                "agent.itinerary.max_tool_calls must be >= 1 "
                f"(got {self.max_tool_calls})"
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
    find_known: FindKnownConfig = FindKnownConfig()
    suggest_places: SuggestPlacesConfig = SuggestPlacesConfig()
    research: ResearchToolConfig = ResearchToolConfig()
    web_search: WebSearchToolConfig = WebSearchToolConfig()
    itinerary: ItineraryConfig = ItineraryConfig()
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
    """Mobility applied when nobody has told kebi how this user gets around
    (ADR-085 / ADR-086, reversed by ADR-156).

    This used to lead with walking, on the reasoning that a conservative guess
    was the safe one. It is not. Narrow-when-ignorant fails *invisibly*: a
    driver capped at walking range never learns that the places beyond it were
    removed, so there is nothing to correct and no signal anything went wrong.
    Guessing wide fails visibly instead — a place turns out to be twenty
    minutes away, the user says so, and the turn recovers.

    `rideshare` leads because it is the one mode almost anyone has almost
    anywhere: it assumes no licence, no vehicle, no fitness, and it reaches
    whatever a car reaches. Walking and transit stay listed so the resolver can
    still pick them when the message or the city calls for it.

    This is a guess, not an answer, and the caller must keep saying so —
    `_mobility_profile` reports it as unresolved, and the prompt is required
    to flag distance in the prose rather than assert it silently.
    """

    reach: Reach = "normal"
    available_modes: list[MovementMode] = ["rideshare", "walking", "transit"]


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
    # Ceiling on the resolved radius (ADR-143). Tier, mode, and density
    # multiply, so the wide end compounded into radii that covered an entire
    # island; beyond this a search is no longer "near" anything.
    max_radius_m: int = 60000


class LLMModelPricing(BaseModel):
    """Per-1M-token rates for one LLM, keyed in `pricing.llm` by model name.

    Usage-dict convention (matches what call sites stamp on spans):
    `input` counts uncached input tokens only; cached tokens ride the
    optional `cache_read_input_tokens` / `cache_creation_input_tokens`
    keys. `output` is completion tokens. `total` is informational and
    never priced.
    """

    input_per_1m: float
    output_per_1m: float
    # OpenAI-style cached-input rate (reads only; writes are free).
    cached_input_per_1m: float | None = None
    # Anthropic-style cache rates. Writes default to the 5m tier — the
    # only tier kebi uses (`cache_control: ephemeral`).
    cache_read_per_1m: float | None = None
    cache_write_5m_per_1m: float | None = None
    cache_write_1h_per_1m: float | None = None

    def cost_for(self, usage: dict[str, int]) -> float:
        input_t = usage.get("input", 0)
        output_t = usage.get("output", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        read_rate = (
            self.cache_read_per_1m
            if self.cache_read_per_1m is not None
            else self.cached_input_per_1m
        )
        cost = input_t * self.input_per_1m + output_t * self.output_per_1m
        # Unknown cache rate → price cached tokens at the full input rate
        # (overcounts rather than hides spend).
        cost += cache_read * (read_rate if read_rate is not None else self.input_per_1m)
        write_rate = (
            self.cache_write_5m_per_1m
            if self.cache_write_5m_per_1m is not None
            else self.input_per_1m
        )
        cost += cache_write * write_rate
        return cost / 1_000_000


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


class ExternalProviderPricing(BaseModel):
    google_places: GooglePlacesPricing
    apify: ApifyPricing


class PricingConfig(BaseModel):
    """Provider rates for cost attribution in Langfuse traces.

    Every section is read by code. `llm` is keyed by model name (exact,
    or a prefix of a date-suffixed model id) and prices the `cost_usd`
    stamped on LLM spans — Langfuse's own catalog stays as the
    reconciliation cross-check (ADR-092). `embeddings`, `transcription`,
    and `external` price providers Langfuse cannot.
    """

    currency: str = "USD"
    llm: dict[str, LLMModelPricing] = {}
    embeddings: dict[str, VoyagePricing] = {}
    transcription: dict[str, WhisperPricing] = {}
    external: ExternalProviderPricing

    def llm_cost_for(
        self, model: str | None, usage: dict[str, int] | None
    ) -> float | None:
        """USD cost for one call, or None when model/usage is unknown.

        Exact model-name key first; otherwise the longest `llm` key that
        prefixes the model id (so `claude-haiku-4-5` prices
        `claude-haiku-4-5-20251001`). Missing entry → None: the span
        goes out unpriced rather than mispriced, and the cost report
        surfaces the gap.
        """
        if not model or not usage:
            return None
        entry = self.llm.get(model)
        if entry is None:
            prefixes = [k for k in self.llm if model.startswith(k)]
            if not prefixes:
                return None
            entry = self.llm[max(prefixes, key=len)]
        return entry.cost_for(usage)


class AppConfig(BaseModel):
    app: AppMeta
    models: dict[str, LLMRoleConfig]
    extraction: ExtractionConfig
    providers: AppProvidersConfig = AppProvidersConfig()
    external_services: ExternalServicesConfig = ExternalServicesConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()
    system_prompts: SystemPromptsConfig = SystemPromptsConfig()
    taste_model: TasteModelConfig = TasteModelConfig()
    memory: MemoryConfig = MemoryConfig()
    home: HomeConfig = HomeConfig()
    areas: AreasConfig = AreasConfig()
    user_intents: UserIntentConfig = UserIntentConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    agent: AgentConfig = AgentConfig()
    movement: MovementConfig = MovementConfig()
    pricing: PricingConfig
    prompts: dict[str, PromptConfig] = {}

    @model_validator(mode="before")
    @classmethod
    def _resolve_role_defaults(cls, data: Any) -> Any:
        """Default-only option resolution (ADR-068/173).

        The env-aware path runs in `get_config()` and replaces optioned
        role blocks with flat dicts before AppConfig sees them. Direct
        `AppConfig(**raw)` calls (e.g. from tests) hit this validator
        instead and resolve every role to its `default` option.
        """
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
            _resolve_model_options(
                data["models"], profiles=data.pop("model_profiles", None)
            )
        return data


# Per-prompt required template-slot registry (feature 027 FR-018a).
# Eager validation at _load_prompts() ensures any missing slot aborts boot.
_REQUIRED_PROMPT_SLOTS: dict[str, list[str]] = {
    "agent": [
        "{location_context}",
        "{time_context}",
        "{movement_context}",
        "{user_profile_context}",
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
        raw["models"] = _resolve_model_options(
            raw.get("models") or {},
            overrides=_model_env_overrides(),
            agent_model=get_env().AGENT_MODEL,
            # Consumed here (ADR-176) — profiles exist only at resolution
            # time; the resolved config carries flat LLMRoleConfig blocks.
            profiles=raw.pop("model_profiles", None),
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
    # OpenRouter — one key for the benchmark model matrix (ADR-173).
    # Unset just means openrouter-provider roles can't be selected.
    OPENROUTER_API_KEY: str | None = None
    APIFY_TOKEN: str | None = None
    # Brave Search — gates the `web_search` tool's backend (ADR-145). Unset
    # selects the null provider: the tool stays bound and callable, comes back
    # empty, and the agent answers from what it knows instead of asserting a
    # fact it could not check.
    BRAVE_API_KEY: str | None = None
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
