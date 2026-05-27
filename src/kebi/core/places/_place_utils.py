"""Shared utilities for constructing PlaceObject from PlaceCore + cache."""

from __future__ import annotations

from .models import PlaceCore, PlaceObject


def escape_like(s: str) -> str:
    """Escape LIKE / ILIKE wildcards (`%` and `_`) in user-controlled
    substrings.

    SQL is already parameterised via SQLAlchemy bind params, so this
    isn't an injection fix — it's a DoS fix. Without the escape, a
    value like `"%_%_%_%_%_%_%"` triggers catastrophic LIKE
    backtracking on Postgres' JSONB `astext` fields. Apply at every
    site that builds `ilike(f"%{user_value}%")`; pair with
    `escape="\\"` on the `ilike` call so Postgres honours the
    backslash escape.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def overlay_with_cache(
    cores: list[PlaceCore],
    cached: dict[str, PlaceObject],
) -> list[PlaceObject]:
    """Merge DB cores with cached/refreshed provider data.

    Curated core fields (name, aliases, tags, category) come from the DB —
    it's authoritative. Location and live fields (rating, hours, phone,
    website, popularity) come from the cached PlaceObject — the provider
    is the source of truth for those, and the DB copy of location is
    wiped by the 30-day TTL cron anyway.
    """
    result: list[PlaceObject] = []
    for core in cores:
        cached_obj = (
            cached.get(core.provider_id) if core.provider_id else None
        )
        if cached_obj is None:
            result.append(PlaceObject(**core.model_dump()))
            continue

        core_data = core.model_dump()
        core_data["location"] = (
            cached_obj.location.model_dump() if cached_obj.location else None
        )
        result.append(
            PlaceObject(
                **core_data,
                rating=cached_obj.rating,
                hours=cached_obj.hours,
                phone=cached_obj.phone,
                website=cached_obj.website,
                popularity=cached_obj.popularity,
                cached_at=cached_obj.cached_at,
            )
        )
    return result
