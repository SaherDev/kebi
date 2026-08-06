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
