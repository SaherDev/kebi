"""Area id codec — one routable path segment for a slash-path geo key.

A geo key is hierarchical by construction (`id/bali/canggu`), which is
exactly wrong for `kebi://area/{id}` links and `GET /v1/areas/{id}` routes:
the slashes read as path structure, and Indonesia's country code being
literally `id` makes the failure vivid. So the key travels encoded — one
opaque, URL-safe segment — and this module is the only place that knows the
wire format. Both callers (the chat linkifier minting URIs, the areas route
resolving them) round-trip through here; nothing else may parse a token.

The encoding is reversible on purpose: a token *is* its key, so no lookup
table exists to drift or miss, and a decoded value is validated against the
geo-key grammar before anything trusts it.
"""

from __future__ import annotations

import base64
import binascii
import re

# The grammar `build_geo_key` guarantees: ISO alpha-2 country, then up to two
# lowercase hyphen-slug segments (city, neighborhood). Enforced on both
# encode (never mint a token for a malformed key) and decode (never hand a
# malformed key to a query).
_GEO_KEY_RE = re.compile(r"^[a-z]{2}(/[a-z0-9][a-z0-9-]*){0,2}$")


def encode_area_id(geo_key: str) -> str:
    """URL-safe token for a canonical geo key.

    Raises ValueError on anything that is not `build_geo_key` output — a
    token must never be minted for a key the decoder would reject.
    """
    key = geo_key.strip("/")
    if not _GEO_KEY_RE.match(key):
        raise ValueError(f"not a canonical geo key: {geo_key!r}")
    return base64.urlsafe_b64encode(key.encode("ascii")).decode("ascii").rstrip("=")


def decode_area_id(token: str) -> str:
    """The geo key a token encodes.

    Raises ValueError when the token is not one this codec minted — wrong
    alphabet, or a payload that is not a canonical geo key. Callers map that
    to their own not-found semantics.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"not an area id: {token!r}") from exc
    if not _GEO_KEY_RE.match(key):
        raise ValueError(f"not an area id: {token!r}")
    return key


def parent_keys(geo_key: str) -> list[str]:
    """Ancestor keys, outermost first: `id/bali/canggu` → [`id`, `id/bali`]."""
    parts = geo_key.strip("/").split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def display_from_slug(slug: str) -> str:
    """Fallback display name for an unprofiled key segment.

    `ho-chi-minh-city` → `Ho Chi Minh City`. A bare country code passes
    through uppercased (`id` → `ID`) — the profiler replaces it with the
    real name on the row's first dressing, so this only shows on the one
    thin first open.
    """
    if len(slug) == 2 and "-" not in slug:
        return slug.upper()
    return " ".join(word.capitalize() for word in slug.split("-"))
