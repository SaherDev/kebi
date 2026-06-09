"""LibraryCursor — the single source of truth for library pagination cursors.

A cursor is the keyset anchor of the browse query: the `(saved_at,
user_place_id)` of the last row of a page. This module owns everything about
it — its fields, how it serialises to/from an opaque token, and how it is
derived from a result row — so no other layer re-implements any of it:

  * the repo applies it as a SQL keyset predicate (typed fields),
  * the service converts between the opaque token and this object,
  * the API layer only ever passes the opaque token through.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime

from .models import SavedPlaceView

_SEP = "|"


@dataclass(frozen=True)
class LibraryCursor:
    """Keyset anchor for `UserPlacesRepo.browse` — `(saved_at, user_place_id)`."""

    saved_at: datetime
    user_place_id: str

    @classmethod
    def from_view(cls, view: SavedPlaceView) -> LibraryCursor:
        """The cursor that resumes browsing *after* this view."""
        return cls(view.user_data.saved_at, view.user_data.user_place_id)

    def encode(self) -> str:
        """Serialise to an opaque, url-safe token."""
        raw = f"{self.saved_at.isoformat()}{_SEP}{self.user_place_id}".encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, token: str) -> LibraryCursor:
        """Parse an opaque token. Raises `ValueError` on any malformed input
        (the API layer lets this surface through the `ValueError → 400`
        handler).
        """
        try:
            raw = base64.urlsafe_b64decode(token.encode()).decode()
            ts, sep, upid = raw.partition(_SEP)
            if not sep or not upid:
                raise ValueError("missing separator")
            return cls(datetime.fromisoformat(ts), upid)
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid library cursor: {token!r}") from exc
