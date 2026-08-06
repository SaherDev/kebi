from datetime import datetime
from enum import Enum as PyEnum
from uuid import uuid4

from sqlalchemy import (
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
    # Location-kinds Step 3: a share's noted areas become their own taste
    # signal, distinct from venue sentiment. AREA_INTEREST carries the area's
    # entity_key in `place_id` and its display name / kind in `metadata`;
    # EXPERIENCE_INTEREST (a route/experience share) carries no place — its
    # experience tags ride `metadata`. Both are positive-only (a share is an
    # interest, never a rejection).
    AREA_INTEREST = "area_interest"
    EXPERIENCE_INTEREST = "experience_interest"


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
    for places, a lowercased hierarchical geo slug (`ae`, `ae/dubai`,
    `ae/dubai/jumeirah`) for country/city/neighborhood — see
    `kebi.core.knowledge.schemas` for the builders. `user_id` is NULL for
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


class AreaEntityType(PyEnum):
    """Kinds of geographic area entity (location-kinds Step 2, widened Step 6).

    Mirrors the geo half of `KnowledgeEntityType` (never `place` — venues
    live in the places catalog).

    Step 6 adds the kinds that make an area answerable at the granularity a
    question actually asks: `region` for provinces and states, `neighborhood`
    for the sub-city choices ("where should I stay?"), and — the pair that
    closes the venue-typed-non-venue hole — `natural_feature` and `street`.
    A pass, a beach, a lagoon and a named street are geography with an
    extent, so they are areas; modelling them that way is what stops them
    being saved as venue rows, which no type-based save guard could do
    (the geocoder types Hai Van Pass and Lang Co Beach identically).

    What is deliberately absent is a `route` kind. A named journey with no
    verifiable footprint stays untrusted and collapses to its containing
    area — the line is whether the provider returns a footprint that
    round-trips, not whether the name sounds route-shaped.
    """

    COUNTRY = "country"
    REGION = "region"
    CITY = "city"
    NEIGHBORHOOD = "neighborhood"
    NATURAL_FEATURE = "natural_feature"
    STREET = "street"


class AreaEntity(Base):
    """One row per verified geographic area — the shared area authority.

    `entity_key` is the exact `build_geo_key` format the knowledge layer
    already uses (`vn`, `vn/hoi-an`), so every existing knowledge_claims
    row attaches to its entity with zero migration. Identity (key, name,
    aliases, hierarchy) is kebi's own and permanent; `provider_id`
    (`google:<place_id>`) is storable indefinitely under provider ToS,
    while geometry (lat/lng/bbox) is provider content — `geo_refreshed_at`
    tracks its age and the area service re-geocodes via the place ID when
    it exceeds the 30-day compliance window (same discipline as
    `places.refreshed_at`).

    `parent_key` is a plain column, no FK — the service ensures the parent
    country row from the same geocode response, but a row must never fail
    to persist because its parent hasn't landed yet.
    """

    __tablename__ = "area_entities"
    __table_args__ = (
        Index("ix_area_entities_parent", "parent_key"),
        Index("ix_area_entities_country", "country_code"),
        Index(
            "ix_area_entities_aliases",
            "aliases",
            postgresql_using="gin",
        ),
    )

    entity_key: Mapped[str] = mapped_column(String, primary_key=True)
    entity_type: Mapped[AreaEntityType] = mapped_column(
        Enum(
            AreaEntityType,
            name="area_entity_type",
            native_enum=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
    )
    # Canonical display name from the geocoder ("Hội An" as romanised by
    # the provider); aliases hold slugged variants seen in the wild so a
    # colloquial spelling can find the row without a geocode call.
    name: Mapped[str] = mapped_column(String, nullable=False)
    aliases: Mapped[list] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    # [min_lat, max_lat, min_lng, max_lng] — the feature's real extent.
    bbox: Mapped[list | None] = mapped_column(  # type: ignore[type-arg]
        JSONB, nullable=True
    )
    # Provider classification ("locality", "administrative_area_level_1",
    # "country") — the verification signal and the settlement-class slot.
    place_type: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_key: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    geo_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
