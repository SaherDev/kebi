"""Tests for LibraryCursor — the single source of truth for library paging."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kebi.core.places._cursor import LibraryCursor
from kebi.core.places.models import (
    LibrarySort,
    LocationContext,
    PlaceCore,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)

_T = datetime(2026, 6, 9, 12, 30, tzinfo=UTC)


def _view(name: str = "Café X", upid: str = "up-9") -> SavedPlaceView:
    return SavedPlaceView(
        place=PlaceCore(id="p1", place_name=name, location=LocationContext()),
        user_data=UserPlace(
            user_place_id=upid,
            user_id="u1",
            place_id="p1",
            source=PlaceSource.manual,
            saved_at=_T,
        ),
    )


def test_round_trip_preserves_fields() -> None:
    cur = LibraryCursor(LibrarySort.recent, _T.isoformat(), "up-abc")
    assert LibraryCursor.decode(cur.encode()) == cur


def test_round_trip_preserves_sort_discriminant() -> None:
    cur = LibraryCursor(LibrarySort.name, "café x", "up-abc")
    decoded = LibraryCursor.decode(cur.encode())
    assert decoded == cur
    assert decoded.sort is LibrarySort.name


def test_token_is_opaque_and_urlsafe() -> None:
    token = LibraryCursor(LibrarySort.recent, _T.isoformat(), "up-1").encode()
    # url-safe base64 — no chars that need escaping in a query string.
    assert "/" not in token and "+" not in token and " " not in token


def test_from_view_recent_anchors_on_saved_at() -> None:
    cur = LibraryCursor.from_view(_view(), LibrarySort.recent)
    assert cur == LibraryCursor(LibrarySort.recent, _T.isoformat(), "up-9")


def test_from_view_name_anchors_on_lowered_place_name() -> None:
    cur = LibraryCursor.from_view(_view(name="Café X"), LibrarySort.name)
    assert cur == LibraryCursor(LibrarySort.name, "café x", "up-9")


@pytest.mark.parametrize(
    "bad",
    [
        "@@notbase64@@",
        "",
        "Zm9v",  # "foo" — no separators
        "bm9zZXBhcmF0b3I=",  # "noseparator"
    ],
)
def test_malformed_token_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid library cursor"):
        LibraryCursor.decode(bad)


def test_unknown_sort_in_token_raises_value_error() -> None:
    # A well-formed token whose sort discriminant is not a LibrarySort.
    import base64

    raw = f"bogus|{_T.isoformat()}|up-1".encode()
    token = base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(ValueError, match="invalid library cursor"):
        LibraryCursor.decode(token)
