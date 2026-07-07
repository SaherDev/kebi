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


def stamp_catalog_identity(
    objects: list[PlaceObject],
    cores_by_provider: dict[str, PlaceCore],
) -> list[PlaceObject]:
    """Fill catalog-owned identity fields on provider objects that lack them.

    A place fetched fresh from the provider carries a `provider_id` but no
    catalog `id` — the row is only assigned one when it is persisted. The
    search service persists on the cold path but must then reconcile the
    in-memory object with the row that was just written, or the object
    escapes with `id=None` and downstream savers/signals (which key strictly
    on `places.id`) cannot attribute it.

    For each object whose `id` is None and whose `provider_id` matches a
    persisted core, return a copy carrying the DB `id` / `created_at` /
    `refreshed_at`. Objects that already have an `id` (the DB-hit path) and
    objects with no matching core pass through unchanged. Curated and live
    fields (name, location, rating, ...) are never touched — the provider and
    cache remain their source of truth; only the catalog-owned identity is
    stamped. RETURNING order is not guaranteed upstream, so matching is by
    `provider_id`, never by position.
    """
    if not cores_by_provider:
        return objects
    result: list[PlaceObject] = []
    for obj in objects:
        core = cores_by_provider.get(obj.provider_id) if obj.provider_id else None
        if obj.id is None and core is not None:
            result.append(
                obj.model_copy(
                    update={
                        "id": core.id,
                        "created_at": core.created_at,
                        "refreshed_at": core.refreshed_at,
                    }
                )
            )
        else:
            result.append(obj)
    return result


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
        cached_obj = cached.get(core.provider_id) if core.provider_id else None
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
