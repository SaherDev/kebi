from datetime import datetime
from enum import Enum as PyEnum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kebi.db.base import Base


class InteractionType(PyEnum):
    """Interaction types for taste model signal tracking (ADR-058)."""

    SAVE = "save"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    # Saving a place kebi recommended — a stronger positive than a passive
    # link-share SAVE, carrying its own taste weight (not the same bucket).
    SAVED_RECOMMENDATION = "saved_recommendation"


class TasteModel(Base):
    """Per-user taste profile: signal_counts + LLM summary (ADR-058)."""

    __tablename__ = "taste_model"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    taste_profile_summary: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    signal_counts: Mapped[dict] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generated_from_log_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Digest of the user's Library-pill snapshot at last regen. Pills are
    # mutable state that write no interaction row, so this — alongside
    # generated_from_log_count — lets the stale-guard detect a like/visit/
    # approve change. Nullable: a NULL never matches a computed digest, so the
    # first post-migration regen always runs once, then stabilises.
    pill_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)


class Interaction(Base):
    """Append-only interaction log for taste model signals (ADR-058)."""

    __tablename__ = "interactions"
    __table_args__ = (
        Index("ix_interactions_user_type", "user_id", "type"),
        Index("ix_interactions_user_created", "user_id", "created_at"),
    )

    # UUID primary key (was sequential int prior to the 2026-05 hardening).
    # Sequential ints leak row counts and are a future IDOR primitive if
    # the column ever surfaces in a response — UUIDs avoid both. The
    # log is append-only, so there are no FK consumers to update.
    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[InteractionType] = mapped_column(
        Enum(
            InteractionType,
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column(  # type: ignore[type-arg]
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserIntent(Base):
    """Append-only store of the user's intent-bearing chat turns (ADR-110).

    Backs the home screen's "what you wanted" recall list — the user's past
    natural-language intents played back verbatim. Kept separate from the
    `interactions` taste-signal log so its row count never perturbs the
    taste-regen thresholds. No foreign key to users (Constitution VI:
    cross-repo boundary). Cleared on both a full wipe and a chat-history
    clear, since the list is surfaced conversation history.
    """

    __tablename__ = "user_intents"
    __table_args__ = (Index("ix_user_intents_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column(  # type: ignore[type-arg]
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KnowledgeEntityType(PyEnum):
    """Entity kinds a knowledge claim can be scoped to (ADR-120)."""

    COUNTRY = "country"
    CITY = "city"
    NEIGHBORHOOD = "neighborhood"
    PLACE = "place"


class KnowledgeSourceType(PyEnum):
    """Where a knowledge claim originated (ADR-120)."""

    SHARED_CONTENT = "shared_content"
    CURATED_EXPERT = "curated_expert"
    KEBI_MESSAGE = "kebi_message"
    USER_MESSAGE = "user_message"
    # Mined from a web-search finding during a turn (ADR-145). Its own value,
    # not folded into shared_content, because trust and staleness differ: a
    # search snippet is one unreviewed page, and dating a claim's origin is
    # what lets a future sweep expire the ones about schedules and prices.
    WEB_SEARCH = "web_search"


class KnowledgeReviewStatus(PyEnum):
    """Approval state of a knowledge claim (ADR-122).

    `approved` is the default — the product trusts every writer today. When
    review turns on, a writer's default becomes config and a reviewer (AI or
    team) moves claims between states; the future read path filters to
    `approved`.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class KnowledgeClaim(Base):
    """One row per world-knowledge claim, entity-scoped (ADR-120).

    `entity_key` is a canonical, collision-proof identifier: `place:<places.id>`
    for places, a hierarchical geo-registry id path (`ae`, `ae/{city_pid}`,
    `ae/{city_pid}/{area_pid}`) for country/city/neighborhood — see
    `kebi.core.geo.registry` for resolution. `user_id` is NULL for
    global claims (shared_content, curated_expert) and set for
    conversation-origin claims (kebi_message, user_message), which are only
    ever read back for that same user. `confidence` is writer-set 0-1;
    convention is curated_expert high, a single harvested mention lower.
    """

    __tablename__ = "knowledge_claims"
    __table_args__ = (
        UniqueConstraint(
            "entity_key",
            "claim",
            "source_type",
            "user_id",
            name="uq_knowledge_claims_dedup",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_knowledge_claims_entity", "entity_type", "entity_key"),
        Index(
            "ix_knowledge_claims_user",
            "user_id",
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index("ix_knowledge_claims_review_status", "review_status"),
        # Author lookups: a curated claim is global (user_id NULL) and its
        # author lives only in source_ref ("curator:{user_id}"), so "my
        # claims" and author-only delete both filter here.
        Index(
            "ix_knowledge_claims_source_ref",
            "source_ref",
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    entity_type: Mapped[KnowledgeEntityType] = mapped_column(
        Enum(
            KnowledgeEntityType,
            name="knowledge_entity_type",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    entity_key: Mapped[str] = mapped_column(String, nullable=False)
    entity_name: Mapped[str] = mapped_column(String, nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    source_type: Mapped[KnowledgeSourceType] = mapped_column(
        Enum(
            KnowledgeSourceType,
            name="knowledge_source_type",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Review gate (ADR-122). Defaults to APPROVED — the product trusts every
    # writer today; when review turns on the default becomes config. reviewed_*
    # stay NULL until an actual review happens (auto-trusted != reviewed).
    review_status: Mapped[KnowledgeReviewStatus] = mapped_column(
        Enum(
            KnowledgeReviewStatus,
            name="knowledge_review_status",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        server_default=KnowledgeReviewStatus.APPROVED.value,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Corroboration tally: how many users agreed vs disagreed with this claim.
    # Both start at 0 and only ever move once the (future) vote write-path
    # ships; surfaced today so the Library note already carries the counts.
    agree_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    disagree_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserMemory(Base):
    """Append-only store of personal facts extracted from user messages.

    Extracted facts are deduped at database level via UNIQUE(user_id, memory).
    No foreign key to users table (Constitution VI: cross-repo boundary).
    """

    __tablename__ = "user_memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "memory",
            name="uq_user_memories_user_memory",
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    memory: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GeoAreaRow(Base):
    """One row per geographic unit kebi has ever seen — the identity registry.

    Identity is the provider's stable place id, minted lazily by one geocoder
    lookup the first time a save or claim names an area the registry doesn't
    know; every later mention worldwide joins by alias lookup with no network.
    Names are data on the row (the provider's clean English `name` plus the
    once-minted colloquial layer), never derived from keys — this table is
    what retired the hand-maintained fold tables in `core.knowledge.schemas`.
    """

    __tablename__ = "geo_areas"
    __table_args__ = (
        UniqueConstraint("geo_key", name="uq_geo_areas_geo_key"),
        # Legacy slug keys resolve old tokens and drive the one-off data
        # migration; partial — rows minted after the migration have none.
        Index(
            "ix_geo_areas_legacy_key",
            "legacy_key",
            postgresql_where=text("legacy_key IS NOT NULL"),
        ),
        # Per-save disambiguation inside an ambiguous unit reads its splits.
        Index(
            "ix_geo_areas_split_of",
            "split_of",
            postgresql_where=text("split_of IS NOT NULL"),
        ),
    )

    place_id: Mapped[str] = mapped_column(String, primary_key=True)
    # Which geocoder minted the id. A future provider is new rows, never a
    # new column meaning — ids are opaque and never compared across providers.
    provider: Mapped[str] = mapped_column(
        String, nullable=False, server_default="google"
    )
    country_code: Mapped[str] = mapped_column(String, nullable=False)
    # Structural position in geo keys: a `city` row is the second segment,
    # an `area` row the third. Distinct from `kind` — Bali is an
    # administrative_area_level_1 by kind but sits in the city slot.
    slot: Mapped[str] = mapped_column(String, nullable=False)
    # The provider's own primary type (locality, administrative_area_level_4,
    # natural_feature, …) — data for display and disambiguation, never a
    # hardcoded vocabulary of ours.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The colloquial layer (LLM-minted once, code-verified): what people call
    # the unit when that differs from the provider's honest name, and the
    # bigger colloquial area it belongs to (Tibubeneng groups into Canggu).
    colloquial_name: Mapped[str | None] = mapped_column(String, nullable=True)
    groups_into: Mapped[str | None] = mapped_column(String, nullable=True)
    # Set on rows minted to subdivide an ambiguous unit (the Gili islands
    # under the Gili Indah desa) — points at that unit's place_id.
    split_of: Mapped[str | None] = mapped_column(String, nullable=True)
    # The city-slot row this area-slot row lives under; NULL on city rows.
    city_place_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The unit's own full id-path key ({cc}/{pid} or {cc}/{city_pid}/{pid}) —
    # stored composed so key lookups and prefix scans never need a join.
    geo_key: Mapped[str] = mapped_column(String, nullable=False)
    # The slug key this unit keyed under before the id migration; decodes
    # tokens minted in old chat messages forever.
    legacy_key: Mapped[str | None] = mapped_column(String, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provider viewport [south, west, north, east] — the geometry per-save
    # disambiguation tests points against.
    viewport: Mapped[list | None] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=True
    )
    # True when one alias name covers several distinctly-named places and a
    # point is needed to tell them apart (resolved via the `split_of` rows).
    ambiguous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    minted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GeoAreaAliasRow(Base):
    """Name-slug → registry row lookup, scoped to its container.

    The slug is a lookup *hint*, never identity: two cities in one country
    can each have a "Chinatown", so area-slot aliases are scoped by the
    containing city row and city-slot aliases use the empty-string scope.
    Rows accrete — every verified way a unit has been asked for lands here,
    so the next ask joins without a geocoder call.
    """

    __tablename__ = "geo_area_aliases"

    country_code: Mapped[str] = mapped_column(String, primary_key=True)
    # '' for a city-slot alias; the containing city's place_id otherwise.
    city_place_id: Mapped[str] = mapped_column(
        String, primary_key=True, server_default=""
    )
    slug: Mapped[str] = mapped_column(String, primary_key=True)
    place_id: Mapped[str] = mapped_column(String, nullable=False, index=True)


class Area(Base):
    """One row per profiled geo entity — the area screen's global half (ADR-153).

    Keyed by the canonical geo key (the geo registry's id path: `id`,
    `id/{city_pid}`, `id/{city_pid}/{area_pid}`), the same identity claims
    already use, so an area's
    claims, links, and screen all resolve through one key. The row exists
    only once the profiler has dressed the area: presence *is* the
    "already profiled" signal, exactly as experiential tags are for places
    (ADR-152). Everything here is user-independent — Jumeirah is Jumeirah
    for everyone; the personal half of the screen is computed per request
    and never stored.
    """

    __tablename__ = "areas"

    geo_key: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Display label ("region", "neighbourhood"), not the key's structural
    # position — Bali sits in the city slot but is not a city.
    level: Mapped[str] = mapped_column(String, nullable=False)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # [{icon, text}] chips, ancestor display names, and
    # [{geo_key, name, icon, hook}] children — shapes owned by
    # `core.areas.models`.
    best_for: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    breadcrumb: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    notable_sub_areas: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    profiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
