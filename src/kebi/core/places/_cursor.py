"""LibraryCursor — the single source of truth for library pagination cursors.

A cursor is the keyset anchor of the browse query: the sort-key value plus the
`user_place_id` of the last row of a page. The anchor depends on the active
sort (see `LibrarySort`):

  * `recent` → `saved_at` (an ISO-8601 instant),
  * `name`   → the case-folded place name (`lower(place_name)`).

The cursor records which sort it was minted under, so the repo can both apply
the right keyset predicate and reject a cursor replayed under a different sort
(switching the toggle restarts paging from the first page). This module owns
everything about the cursor — its fields, its opaque serialisation, and how it
is derived from a result row — so no other layer re-implements any of it:

  * the repo applies it as a SQL keyset predicate (typed via the sort spec),
  * the service converts between the opaque token and this object,
  * the API layer only ever passes the opaque token through.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from .models import LibrarySort, SavedPlaceView

_SEP = "|"


@dataclass(frozen=True)
class LibraryCursor:
    """Keyset anchor for `UserPlacesRepo.browse`.

    `anchor` is the serialised primary sort-key of the last row of a page —
    an ISO-8601 `saved_at` for `recent`, the case-folded place name for
    `name`. `user_place_id` is the stable tie-break. `sort` records which
    ordering produced it.
    """

    sort: LibrarySort
    anchor: str
    user_place_id: str

    @classmethod
    def from_view(cls, view: SavedPlaceView, sort: LibrarySort) -> LibraryCursor:
        """The cursor that resumes browsing *after* this view under `sort`."""
        if sort is LibrarySort.name:
            anchor = view.place.place_name.lower()
        else:
            anchor = view.user_data.saved_at.isoformat()
        return cls(sort, anchor, view.user_data.user_place_id)

    def encode(self) -> str:
        """Serialise to an opaque, url-safe token."""
        raw = f"{self.sort.value}{_SEP}{self.anchor}{_SEP}{self.user_place_id}".encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, token: str) -> LibraryCursor:
        """Parse an opaque token. Raises `ValueError` on any malformed input —
        an unknown sort, a missing field, or undecodable bytes (the API layer
        lets this surface through the `ValueError → 400` handler).
        """
        try:
            raw = base64.urlsafe_b64decode(token.encode()).decode()
            sort_raw, sep1, rest = raw.partition(_SEP)
            anchor, sep2, upid = rest.partition(_SEP)
            if not sep1 or not sep2 or not anchor or not upid:
                raise ValueError("missing field")
            return cls(LibrarySort(sort_raw), anchor, upid)
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid library cursor: {token!r}") from exc
