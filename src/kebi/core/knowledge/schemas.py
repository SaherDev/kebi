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
    created_at: datetime


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


class HarvestSnapshot(BaseModel):
    """The full bucket payload for one harvestable extraction event —
    content plus the places it produced. Written to object storage under a
    `harvest/{request_id}.json` key; the harvest event carries only the key,
    and the handler reads this back."""

    model_config = ConfigDict(frozen=True)

    content: HarvestContent
    places: list[HarvestPlace] = []


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
