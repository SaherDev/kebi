"""Tests for `stamp_catalog_identity` — reconciling provider objects with
their persisted catalog identity.

A place fetched fresh from the provider carries a `provider_id` but no
catalog `id` (the row is only assigned one on persist). The search service
persists on the cold path, then stamps the DB-assigned `id` back onto the
in-memory object so it doesn't escape with `id=None` — which would break
downstream save/signal, both of which key strictly on `places.id`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kebi.core.places._place_utils import stamp_catalog_identity
from kebi.core.places.models import LocationContext, PlaceCore, PlaceObject


def _provider_object(pid: str, id_: str | None = None) -> PlaceObject:
    """A provider-fetched object: has provider_id, id defaults to None."""
    return PlaceObject(
        id=id_,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=LocationContext(lat=1.0, lng=2.0, address="Provider St"),
        rating=4.5,
    )


def _persisted_core(pid: str) -> PlaceCore:
    """A catalog row as returned by the upsert: carries the minted id."""
    return PlaceCore(
        id=f"uuid-{pid}",
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        refreshed_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_stamps_id_onto_idless_object() -> None:
    obj = _provider_object("a")
    cores = {"google:a": _persisted_core("a")}

    result = stamp_catalog_identity([obj], cores)

    assert result[0].id == "uuid-a"
    assert result[0].created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert result[0].refreshed_at == datetime(2026, 6, 1, tzinfo=UTC)


def test_never_touches_curated_or_live_fields() -> None:
    """Only catalog identity is stamped; name/location/rating are the
    provider's to own."""
    obj = _provider_object("a")
    # The persisted core deliberately carries a divergent name to prove it
    # does not leak into the returned object.
    core = _persisted_core("a").model_copy(update={"place_name": "DB Name"})

    result = stamp_catalog_identity([obj], {"google:a": core})

    assert result[0].place_name == "Place a"
    assert result[0].location is not None
    assert result[0].location.lat == 1.0
    assert result[0].rating == 4.5


def test_leaves_object_with_existing_id_untouched() -> None:
    """DB-hit path: the object already carries its id — pass it through as-is,
    even if a core is present."""
    obj = _provider_object("a", id_="original-id")
    cores = {"google:a": _persisted_core("a")}

    result = stamp_catalog_identity([obj], cores)

    assert result[0] is obj
    assert result[0].id == "original-id"


def test_unmatched_provider_id_passes_through() -> None:
    obj = _provider_object("a")
    cores = {"google:other": _persisted_core("other")}

    result = stamp_catalog_identity([obj], cores)

    assert result[0] is obj
    assert result[0].id is None


def test_empty_core_map_returns_objects_unchanged() -> None:
    objs = [_provider_object("a"), _provider_object("b")]

    result = stamp_catalog_identity(objs, {})

    assert result is objs


def test_object_without_provider_id_is_not_stamped() -> None:
    anon = PlaceObject(id=None, provider_id=None, place_name="Anonymous")
    result = stamp_catalog_identity([anon], {"google:a": _persisted_core("a")})

    assert result[0].id is None


def test_mixed_batch_stamps_only_idless_matches() -> None:
    idless = _provider_object("a")
    already = _provider_object("b", id_="b-id")
    unmatched = _provider_object("c")
    cores = {"google:a": _persisted_core("a"), "google:b": _persisted_core("b")}

    result = stamp_catalog_identity([idless, already, unmatched], cores)

    assert result[0].id == "uuid-a"  # stamped
    assert result[1].id == "b-id"  # untouched (had id)
    assert result[2].id is None  # unmatched
