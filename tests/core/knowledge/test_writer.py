"""Tests for KnowledgeWriter — key building, drops, confidence floor (ADR-121)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kebi.core.knowledge.schemas import ResolvedGeo, StructuredClaim
from kebi.core.knowledge.writer import KnowledgeWriter

_UAE = ResolvedGeo(country_code="ae", city="Dubai", neighborhood="Jumeirah")


@dataclass
class _FakeRepo:
    """Records save() calls; treats an identical dedup tuple as a collision."""

    saved: list[dict] = field(default_factory=list)
    _seen: set[tuple] = field(default_factory=set)

    async def save(
        self,
        entity_type,
        entity_key,
        entity_name,
        claim,
        source_type,
        confidence,
        tags=None,
        source_ref=None,
        user_id=None,
        review_status="approved",
    ) -> str | None:
        dedup = (entity_key, claim, source_type, user_id)
        row = {
            "entity_type": entity_type,
            "entity_key": entity_key,
            "entity_name": entity_name,
            "claim": claim,
            "source_type": source_type,
            "confidence": confidence,
            "tags": tags or [],
            "source_ref": source_ref,
            "user_id": user_id,
            "review_status": review_status,
        }
        self.saved.append(row)
        if dedup in self._seen:
            return None
        self._seen.add(dedup)
        return f"id-{len(self._seen)}"


def _claim(scope, *, geo=_UAE, place_ref=None, tags=None, confidence=0.5, name="X"):
    return StructuredClaim(
        scope=scope,
        entity_name=name,
        claim=f"a {scope} fact",
        tags=tags or [],
        confidence=confidence,
        place_ref=place_ref,
        geo=None if scope == "place" else geo,
    )


async def _persist(repo, claims, *, floor=0.35, source_type="shared_content"):
    return await KnowledgeWriter(repo).persist(
        claims,
        source_type=source_type,
        source_ref="ref",
        user_id=None,
        confidence_floor=floor,
    )


async def test_builds_place_and_geo_keys() -> None:
    repo = _FakeRepo()
    written = await _persist(
        repo,
        [
            _claim("place", place_ref="p1"),
            _claim("country"),
            _claim("city"),
            _claim("neighborhood"),
        ],
    )
    keys = [r["entity_key"] for r in repo.saved]
    assert keys == ["place:p1", "ae", "ae/dubai", "ae/dubai/jumeirah"]
    assert len(written) == 4
    # Each written claim carries the id its row was created with.
    assert all(w.id for w in written)
    assert [w.claim.scope for w in written] == [
        "place",
        "country",
        "city",
        "neighborhood",
    ]


async def test_drops_claim_with_no_country_code() -> None:
    repo = _FakeRepo()
    written = await _persist(
        repo, [_claim("city", geo=ResolvedGeo(country_code=None, city="Dubai"))]
    )
    assert repo.saved == []
    assert written == []


async def test_drops_place_claim_without_place_ref() -> None:
    repo = _FakeRepo()
    written = await _persist(repo, [_claim("place", place_ref=None)])
    assert written == []


async def test_drops_city_claim_missing_city() -> None:
    repo = _FakeRepo()
    geo = ResolvedGeo(country_code="ae", city=None)
    assert await _persist(repo, [_claim("city", geo=geo)]) == []


async def test_drops_accessibility_claim() -> None:
    repo = _FakeRepo()
    written = await _persist(
        repo, [_claim("place", place_ref="p1", tags=["wheelchair-accessible"])]
    )
    assert written == []
    assert repo.saved == []


async def test_tags_normalized_to_vocabulary_on_write() -> None:
    """Known tags stored in canonical form; off-vocab hallucinations dropped."""
    repo = _FakeRepo()
    written = await _persist(
        repo,
        [
            _claim(
                "city",
                tags=["thai", "banana-pancake-street", "cash only", "GO_EARLY"],
            )
        ],
    )
    assert repo.saved[0]["tags"] == ["Thai", "cash_only", "go_early"]
    # The returned claim echoes what was stored, not the raw emission.
    assert written[0].claim.tags == ["Thai", "cash_only", "go_early"]


async def test_accessibility_checked_on_raw_tags_before_normalization() -> None:
    """An accessibility marker in a raw (even off-vocab) tag still drops the
    whole claim — normalization must not launder it out first."""
    repo = _FakeRepo()
    written = await _persist(
        repo, [_claim("city", tags=["step-free entrance", "cash_only"])]
    )
    assert written == []
    assert repo.saved == []


async def test_confidence_floored_by_source_trust() -> None:
    repo = _FakeRepo()
    await _persist(repo, [_claim("country", confidence=0.1)], floor=0.9)
    assert repo.saved[0]["confidence"] == 0.9


async def test_model_confidence_wins_above_floor() -> None:
    repo = _FakeRepo()
    await _persist(repo, [_claim("country", confidence=0.8)], floor=0.35)
    assert repo.saved[0]["confidence"] == 0.8


async def test_dedup_only_counts_new_rows() -> None:
    repo = _FakeRepo()
    first = await _persist(repo, [_claim("country")])
    again = await _persist(repo, [_claim("country")])
    assert len(first) == 1
    assert again == []  # identical claim collapses on the dedup key


async def test_review_status_defaults_approved_and_passes_through() -> None:
    repo = _FakeRepo()
    await KnowledgeWriter(repo).persist(
        [_claim("country")],
        source_type="shared_content",
        source_ref="r",
        user_id=None,
        confidence_floor=0.35,
    )
    assert repo.saved[0]["review_status"] == "approved"


async def test_review_status_pending_passes_through() -> None:
    repo = _FakeRepo()
    await KnowledgeWriter(repo).persist(
        [_claim("country")],
        source_type="shared_content",
        source_ref="r",
        user_id=None,
        confidence_floor=0.35,
        review_status="pending",
    )
    assert repo.saved[0]["review_status"] == "pending"
