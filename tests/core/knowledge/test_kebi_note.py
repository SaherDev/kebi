"""Tests for the kebi_message writer + the place-notes reader (ADR-127).

Covers the third ClaimProducer (KebiNoteProducer), its thin orchestration
(KebiNoteService), and the knowledge layer's first reader (PlaceNotesService):
place-scoped pull, from_shared flagging, ranking/limit, and approved-only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from kebi.core.knowledge.kebi_note import KebiNoteProducer
from kebi.core.knowledge.kebi_note_service import KebiNoteService
from kebi.core.knowledge.place_notes_service import PlaceNotesService
from kebi.core.knowledge.producer import ClaimProducer
from kebi.core.knowledge.schemas import KnowledgeClaim, SourceType
from kebi.core.places.models import PlaceCore, PlaceSource, SavedPlaceView, UserPlace

# ---- producer --------------------------------------------------------------


def test_producer_is_a_claim_producer() -> None:
    producer = KebiNoteProducer(confidence_floor=0.8)
    assert isinstance(producer, ClaimProducer)
    assert producer.source_type == "kebi_message"
    assert producer.confidence_floor == 0.8
    assert producer.review_status == "approved"


def test_note_builds_place_scoped_claim() -> None:
    producer = KebiNoteProducer(confidence_floor=0.8)

    claims = producer.note("great for a quiet date", place_id="p1", place_name="Nara")

    assert len(claims) == 1
    claim = claims[0]
    assert claim.scope == "place"
    assert claim.place_ref == "p1"
    assert claim.entity_name == "Nara"
    assert claim.claim == "great for a quiet date"
    assert claim.confidence == 0.8


def test_blank_reason_yields_no_claim() -> None:
    producer = KebiNoteProducer(confidence_floor=0.8)
    assert producer.note("   ", place_id="p1", place_name="Nara") == []


# ---- service ---------------------------------------------------------------


async def test_record_ingests_user_scoped_with_recommendation_provenance() -> None:
    ingestion = AsyncMock(ingest=AsyncMock(return_value=[]))
    producer = KebiNoteProducer(confidence_floor=0.8)
    service = KebiNoteService(producer, ingestion)

    await service.record(
        reason="great for a quiet date",
        place_id="p1",
        place_name="Nara",
        user_id="user_1",
        recommendation_id="rec-9",
    )

    ingestion.ingest.assert_awaited_once()
    args, kwargs = ingestion.ingest.await_args
    assert args[0] is producer
    assert len(args[1]) == 1  # the structured claim
    assert kwargs["source_ref"] == "kebi:rec:rec-9"
    assert kwargs["user_id"] == "user_1"


# ---- reader ----------------------------------------------------------------


class _FakeRepo:
    """Captures the read call and returns a canned claim list."""

    def __init__(self, claims: list[KnowledgeClaim]) -> None:
        self._claims = claims
        self.last_kwargs: dict[str, object] = {}

    async def list_for_entities(
        self,
        entity_keys: list[str],
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]:
        self.last_kwargs = {
            "entity_keys": entity_keys,
            "user_id": user_id,
            "approved_only": approved_only,
        }
        return [c for c in self._claims if c.entity_key in entity_keys]


def _claim(
    entity_key: str,
    claim: str,
    *,
    confidence: float,
    source_type: SourceType = "shared_content",
    source_ref: str | None = None,
) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=f"c-{claim}",
        entity_type="place",
        entity_key=entity_key,
        entity_name="Nara",
        claim=claim,
        source_type=source_type,
        source_ref=source_ref,
        confidence=confidence,
        created_at=datetime(2026, 7, 11, tzinfo=UTC),
    )


def _save(place_id: str, source_ref: str | None) -> SavedPlaceView:
    # A URL source_ref requires a share source; None requires an internal one.
    source = PlaceSource.tiktok if source_ref else PlaceSource.manual
    return SavedPlaceView(
        place=PlaceCore(id=place_id, place_name="Nara"),
        user_data=UserPlace(
            user_place_id=f"up-{place_id}",
            user_id="user_1",
            place_id=place_id,
            approved=True,
            source=source,
            source_ref=source_ref,
            saved_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    )


async def test_from_shared_flags_notes_mined_from_the_users_own_post() -> None:
    repo = _FakeRepo(
        [
            _claim("place:p1", "order the omakase", confidence=0.4, source_ref="vid"),
            _claim("place:p1", "generally known", confidence=0.9, source_ref="other"),
        ]
    )
    service = PlaceNotesService(repo, limit=6)  # type: ignore[arg-type]

    notes = await service.notes_for_saves([_save("p1", "vid")], "user_1")

    by_text = {n.text: n for n in notes["p1"]}
    assert by_text["order the omakase"].from_shared is True
    assert by_text["generally known"].from_shared is False


async def test_notes_ranked_by_confidence_and_capped() -> None:
    repo = _FakeRepo(
        [
            _claim("place:p1", "weak", confidence=0.4),
            _claim("place:p1", "strong", confidence=0.95),
            _claim("place:p1", "mid", confidence=0.6),
        ]
    )
    service = PlaceNotesService(repo, limit=2)  # type: ignore[arg-type]

    notes = await service.notes_for_saves([_save("p1", None)], "user_1")

    assert [n.text for n in notes["p1"]] == ["strong", "mid"]


async def test_reader_requests_only_approved_and_scopes_to_user() -> None:
    repo = _FakeRepo([])
    service = PlaceNotesService(repo, limit=6)  # type: ignore[arg-type]

    await service.notes_for_saves([_save("p1", None)], "user_1")

    assert repo.last_kwargs["approved_only"] is True
    assert repo.last_kwargs["user_id"] == "user_1"
    assert repo.last_kwargs["entity_keys"] == ["place:p1"]


async def test_place_with_no_claims_gets_empty_list() -> None:
    repo = _FakeRepo([])
    service = PlaceNotesService(repo, limit=6)  # type: ignore[arg-type]

    notes = await service.notes_for_saves([_save("p1", "vid")], "user_1")

    assert notes["p1"] == []


# ---- end-to-end: real write seam → real reader over one store --------------


class _InMemoryRepo:
    """A real-enough claim store: honours the dedup key, user-scoping, and the
    approved_only filter, so the actual writer + reader run against it."""

    def __init__(self) -> None:
        self.rows: list[KnowledgeClaim] = []

    async def save(
        self,
        entity_type: str,
        entity_key: str,
        entity_name: str,
        claim: str,
        source_type: SourceType,
        confidence: float,
        tags: list[str] | None = None,
        source_ref: str | None = None,
        user_id: str | None = None,
        review_status: str = "approved",
    ) -> bool:
        key = (entity_key, claim, source_type, user_id)
        if any(
            (r.entity_key, r.claim, r.source_type, r.user_id) == key for r in self.rows
        ):
            return False
        self.rows.append(
            KnowledgeClaim(
                id=f"row-{len(self.rows)}",
                entity_type=entity_type,  # type: ignore[arg-type]
                entity_key=entity_key,
                entity_name=entity_name,
                claim=claim,
                tags=tags or [],
                source_type=source_type,
                source_ref=source_ref,
                confidence=confidence,
                user_id=user_id,
                review_status=review_status,  # type: ignore[arg-type]
                created_at=datetime(2026, 7, 14, tzinfo=UTC),
            )
        )
        return True

    async def list_for_entities(
        self,
        entity_keys: list[str],
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]:
        out = []
        for r in self.rows:
            if r.entity_key not in entity_keys:
                continue
            if r.user_id is not None and r.user_id != user_id:
                continue
            if approved_only and r.review_status != "approved":
                continue
            out.append(r)
        return out


async def test_end_to_end_save_reason_then_read_it_back() -> None:
    """A saved reason flows through the real producer→ingestion→writer seam
    into the store, and the real reader surfaces it on the Library alongside a
    harvested community note — with from_shared set only on the shared post."""
    from kebi.core.knowledge.producer import KnowledgeIngestion
    from kebi.core.knowledge.writer import KnowledgeWriter

    repo = _InMemoryRepo()
    ingestion = KnowledgeIngestion(KnowledgeWriter(repo))  # type: ignore[arg-type]
    note_service = KebiNoteService(KebiNoteProducer(confidence_floor=0.8), ingestion)

    # A harvested (global) note about the place, mined from the shared post.
    await repo.save(
        entity_type="place",
        entity_key="place:p1",
        entity_name="Nara",
        claim="order the omakase",
        source_type="shared_content",
        confidence=0.4,
        source_ref="vid",
        user_id=None,
    )
    # The user saves the pick with a reason → a user-scoped kebi_message claim.
    await note_service.record(
        reason="great for a quiet date",
        place_id="p1",
        place_name="Nara",
        user_id="user_1",
        recommendation_id="r1",
    )

    reader = PlaceNotesService(repo, limit=6)  # type: ignore[arg-type]
    notes = await reader.notes_for_saves([_save("p1", "vid")], "user_1")

    by_text = {n.text: n for n in notes["p1"]}
    assert by_text["great for a quiet date"].source_type == "kebi_message"
    assert by_text["great for a quiet date"].from_shared is False
    assert by_text["order the omakase"].source_type == "shared_content"
    assert by_text["order the omakase"].from_shared is True

    # Another user never sees the first user's kebi_message reason.
    other = await reader.notes_for_saves([_save("p1", "vid")], "user_2")
    assert [n.text for n in other["p1"]] == ["order the omakase"]
