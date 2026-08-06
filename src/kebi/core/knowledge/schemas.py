"""Domain models for the knowledge layer's claims store (ADR-120).

The knowledge layer holds what exists in the world — country, city,
neighborhood, and place facts — regardless of where they came from. The
taste layer (`core.taste`, `core.user.intent_models`, `user_memories`) is
the separate counterpart: what *this user* cares about. The agent reads
both at query time; neither layer knows the agent's mode.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from anyascii import anyascii
from pydantic import BaseModel, ConfigDict

EntityType = Literal["country", "city", "neighborhood", "place"]
SourceType = Literal["shared_content", "curated_expert", "kebi_message", "user_message"]
ReviewStatus = Literal["pending", "approved", "rejected"]

# Coarse, user-facing origin label for a surfaced claim (ADR-127). The raw
# `source_type` never crosses the wire; every reader (Library notes, research
# notes) maps through this one table so the labels can't diverge.
NOTE_SOURCE_LABELS: dict[SourceType, str] = {
    "shared_content": "community",
    "curated_expert": "expert",
    "kebi_message": "kebi",
    "user_message": "kebi",
}


def note_source_label(source_type: SourceType) -> str:
    """The coarse origin label for a claim's source_type."""
    return NOTE_SOURCE_LABELS.get(source_type, "community")


_COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}$")


class KnowledgeClaim(BaseModel):
    """One claim about an entity, plus its provenance.

    Read/write boundary model — never the ORM row. `user_id` is None for a
    global claim (shared_content, curated_expert) and set for a
    conversation-origin claim (kebi_message, user_message), which is only
    ever read back for that same user.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    entity_type: EntityType
    entity_key: str
    entity_name: str
    claim: str
    tags: list[str] = []
    source_type: SourceType
    source_ref: str | None = None
    confidence: float
    user_id: str | None = None
    # Review gate (ADR-122). Defaults to approved (trust-everything today);
    # reviewed_by/reviewed_at are set only once an AI or team member reviews.
    review_status: ReviewStatus = "approved"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    # Corroboration tally (agree/disagree). Both 0 until the vote write-path
    # ships; carried through so the Library note can show the counts today.
    agree_count: int = 0
    disagree_count: int = 0
    created_at: datetime


class PlaceNote(BaseModel):
    """One insider note surfaced on a place — a claim reduced to what the
    Library read exposes (ADR-127).

    `id` is the underlying claim's id — a stable key for the client's list and
    the target the (future) agree/disagree vote will address. `agree_count` /
    `disagree_count` are its corroboration tally (0 until voting ships).
    `source_type` is kept internal so the API layer can map it to a coarse,
    user-facing label; it is not itself a wire field. `from_shared` is True
    when the underlying claim was harvested from the very post the user shared
    for this save (its `source_ref` equals the save's `source_ref`), so the
    client can badge "from what you shared" without any grouping server-side.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    tags: list[str] = []
    source_type: SourceType
    from_shared: bool = False
    agree_count: int = 0
    disagree_count: int = 0


def note_rank_key(claim: KnowledgeClaim) -> tuple[float, datetime]:
    """Sort key for surfacing claims as notes: strongest, then most recent.

    Shared by every reader that turns claims into notes (saved places, areas)
    so "the best notes" means one thing across the product rather than
    whatever each reader happened to sort by. Use with `reverse=True`.
    """
    return (claim.confidence, claim.created_at)


class ResolvedGeo(BaseModel):
    """Geo an entity is anchored to, already resolved to canonical parts.

    `country_code` is an ISO-3166 alpha-2 code (what `build_geo_key`
    requires); `city`/`neighborhood` are display names the key builder
    slugifies. Harvest fills this from a place's stored location; curation
    fills it from a geocoder. A claim whose geo lacks the parts its scope
    needs is dropped by the writer rather than mis-keyed.
    """

    model_config = ConfigDict(frozen=True)

    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None


class StructuredClaim(BaseModel):
    """A claim resolved to the point where an entity_key can be built.

    The shared hand-off both writers (harvest, curation) produce and the
    `KnowledgeWriter` consumes. The producing LLM never emits this directly
    with a key — its raw output is resolved (place index → catalog id, prose
    → geo) into this shape first, so a model can never invent a key.
    `confidence` is the model's own 0-1 estimate; the writer floors it by
    source trust. `scope` reuses `EntityType`.
    """

    model_config = ConfigDict(frozen=True)

    scope: EntityType
    entity_name: str
    claim: str
    tags: list[str] = []
    confidence: float
    place_ref: str | None = None
    geo: ResolvedGeo | None = None


class HarvestPlace(BaseModel):
    """A place identified during extraction, with its resolved geo, that the
    harvester reasons about. `place_id` is the catalog id used to key
    place-scoped claims; `geo` anchors any geo-scoped claim about it."""

    model_config = ConfigDict(frozen=True)

    place_id: str
    name: str
    geo: ResolvedGeo


class HarvestContent(BaseModel):
    """The already-gathered content of one share, snapshotted for the
    second pass. No raw media — only the derived text extraction already
    produced (ADR: reuse content, no re-fetch)."""

    model_config = ConfigDict(frozen=True)

    caption: str | None = None
    transcript: str | None = None
    hashtags: list[str] = []
    title: str | None = None
    platform: str | None = None
    location_tag: str | None = None
    source_ref: str | None = None

    def is_empty(self) -> bool:
        """True when there is nothing worth harvesting from."""
        return not any(
            (
                self.caption,
                self.transcript,
                self.title,
                self.location_tag,
                self.hashtags,
            )
        )


class NotedAreaRef(BaseModel):
    """A non-venue name the extraction noted, with the location context the
    share placed it in (ADR-082 per-candidate area).

    The harvest's missing anchor for zero-venue shares: the name resolves
    through the area service — itself when it IS an area ("Hoi An"), its
    containing area when it is a route ("Ha Giang Loop") — and the resolved
    entity anchors the share's claims. `reason` is the validator's
    rejection subtype (`non_venue_route` / `non_venue_area`, or the generic
    `non_venue` from the picker), which lets resolution skip the
    name-as-area probe for routes."""

    model_config = ConfigDict(frozen=True)

    name: str
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    reason: str = "non_venue"


class HarvestSnapshot(BaseModel):
    """The full bucket payload for one harvestable extraction event —
    content plus the places it produced and the non-venue names it noted.
    Written to object storage under a `harvest/{request_id}.json` key; the
    harvest event carries only the key, and the handler reads this back."""

    model_config = ConfigDict(frozen=True)

    content: HarvestContent
    places: list[HarvestPlace] = []
    noted_areas: list[NotedAreaRef] = []


def _slugify(part: str) -> str:
    """Diacritic- and script-insensitive slug so one place keys the same
    regardless of how its name is written.

    `anyascii` transliterates any script to ASCII first, so a name in its local
    script and its romanised spelling collapse to one stable key: "Hội An" and
    "Hoi An" → "hoi-an"; "Đà Nẵng" and "Da Nang" → "da-nang"; "東京" →
    "dongjing"; "กรุงเทพ" → "krungethph". The ASCII result is lowercased and
    every run of non-alphanumeric characters becomes a single hyphen.
    """
    out: list[str] = []
    prev_hyphen = False
    for ch in anyascii(part).lower():
        if ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")


def slugs_match(a: str | None, b: str | None) -> bool:
    """True when two names collapse to the same canonical slug — the
    diacritic- and script-insensitive equality the key builder uses, so
    "Hội An" matches "Hoi An"."""
    if not a or not b:
        return False
    return _slugify(a) == _slugify(b)


def build_place_key(place_id: str) -> str:
    """Canonical entity_key for a place: its catalog id, namespaced."""
    return f"place:{place_id}"


def build_geo_key(
    country: str, city: str | None = None, neighborhood: str | None = None
) -> str:
    """Canonical entity_key for a geo entity: a lowercased hierarchical slug.

    `country` must be an ISO-3166 alpha-2 code (e.g. "ae"). A neighborhood
    key requires a city; "all claims under Dubai" is a prefix scan on the
    city-level key this returns for a city alone.
    """
    country_code = country.strip().lower()
    if not _COUNTRY_CODE_RE.match(country_code):
        raise ValueError(f"country must be an ISO-3166 alpha-2 code, got {country!r}")
    if neighborhood is not None and city is None:
        raise ValueError("neighborhood key requires a city")

    parts = [country_code]
    if city is not None:
        parts.append(_slugify(city))
    if neighborhood is not None:
        parts.append(_slugify(neighborhood))
    return "/".join(parts)
