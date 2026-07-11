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

from pydantic import BaseModel, ConfigDict

EntityType = Literal["country", "city", "neighborhood", "place"]
SourceType = Literal["shared_content", "curated_expert", "kebi_message", "user_message"]

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
    created_at: datetime


def _slugify(part: str) -> str:
    return part.strip().lower().replace(" ", "-")


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
