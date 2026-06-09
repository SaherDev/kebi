"""Tests for LibraryCursor — the single source of truth for library paging."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kebi.core.places._cursor import LibraryCursor
from kebi.core.places.models import (
    LocationContext,
    PlaceCore,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)


def test_round_trip_preserves_fields() -> None:
    cur = LibraryCursor(datetime(2026, 6, 9, 12, 30, tzinfo=UTC), "up-abc")
    assert LibraryCursor.decode(cur.encode()) == cur


def test_token_is_opaque_and_urlsafe() -> None:
    token = LibraryCursor(datetime(2026, 6, 9, tzinfo=UTC), "up-1").encode()
    # url-safe base64 — no chars that need escaping in a query string.
    assert "/" not in token and "+" not in token and " " not in token


def test_from_view_anchors_on_user_data() -> None:
    t = datetime(2026, 6, 9, tzinfo=UTC)
    view = SavedPlaceView(
        place=PlaceCore(id="p1", place_name="X", location=LocationContext()),
        user_data=UserPlace(
            user_place_id="up-9",
            user_id="u1",
            place_id="p1",
            source=PlaceSource.manual,
            saved_at=t,
        ),
    )
    assert LibraryCursor.from_view(view) == LibraryCursor(t, "up-9")


@pytest.mark.parametrize(
    "bad",
    ["@@notbase64@@", "", "Zm9v", "bm9zZXBhcmF0b3I="],  # garbage / no separator
)
def test_malformed_token_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid library cursor"):
        LibraryCursor.decode(bad)
