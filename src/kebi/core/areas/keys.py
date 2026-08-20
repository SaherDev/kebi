"""Area id codec — one routable path segment for a slash-path geo key.

A geo key is hierarchical by construction (`id/{city_pid}/{area_pid}` —
provider place-id segments under an ISO country code), which is exactly
wrong for `kebi://area/{id}` links and `GET /v1/areas/{id}` routes: the
slashes read as path structure. So the key travels encoded — one opaque,
URL-safe segment — and this module is the only place that knows the wire
format. Both callers (the chat linkifier minting URIs, the areas route
resolving them) round-trip through here; nothing else may parse a token.

The encoding is reversible on purpose: a token *is* its key, so no lookup
table exists to drift or miss, and a decoded value is validated against the
geo-key grammar before anything trusts it. Tokens minted before the id
migration decode to the old slug grammar — `is_legacy_geo_key` spots them so
the route can resolve them through the registry's stored legacy keys, which
is what keeps every area link in an old chat message alive.
"""

from __future__ import annotations

import base64
import binascii
import re

# The id-key grammar: ISO alpha-2 country, then up to two provider place-id
# segments (URL-safe alphabet). Enforced on both encode (never mint a token
# for a malformed key) and decode (never hand a malformed key to a query).
# Legacy slug keys are a lexical subset — all-lowercase hyphen slugs — which
# is what lets one decoder serve both eras.
_GEO_KEY_RE = re.compile(r"^[a-z]{2}(/[A-Za-z0-9_-]+){0,2}$")
# The pre-migration slug grammar. A provider place id in practice always
# carries an uppercase character, so a multi-segment key that matches this
# is a legacy key; a bare country code is valid in both eras and needs no
# translation.
_LEGACY_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def encode_area_id(geo_key: str) -> str:
    """URL-safe token for a canonical geo key.

    Raises ValueError on anything that is not a well-formed key — a token
    must never be minted for a key the decoder would reject.
    """
    key = geo_key.strip("/")
    if not _GEO_KEY_RE.match(key):
        raise ValueError(f"not a canonical geo key: {geo_key!r}")
    return base64.urlsafe_b64encode(key.encode("ascii")).decode("ascii").rstrip("=")


def decode_area_id(token: str) -> str:
    """The geo key a token encodes — current or legacy grammar.

    Raises ValueError when the token is not one this codec minted — wrong
    alphabet, or a payload that is not a well-formed key. Callers map that
    to their own not-found semantics, and check `is_legacy_geo_key` on the
    result to know whether a registry translation is needed.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"not an area id: {token!r}") from exc
    if not _GEO_KEY_RE.match(key):
        raise ValueError(f"not an area id: {token!r}")
    return key


def is_geo_key(value: str) -> bool:
    """Whether a string is a well-formed geo key (either era).

    The public form of the grammar this module already enforces on encode and
    decode. Callers that accept a key straight off the wire (an `?area=`
    filter, say) validate here rather than re-deriving the pattern.
    """
    return bool(_GEO_KEY_RE.match(value.strip("/")))


def is_legacy_geo_key(value: str) -> bool:
    """Whether a well-formed key is from the pre-migration slug grammar.

    True only for multi-segment all-lowercase slug paths — the keys minted
    before identity moved to provider ids. A bare country code is not
    legacy: it means the same thing in both eras.
    """
    parts = value.strip("/").split("/")
    if len(parts) < 2:
        return False
    return all(_LEGACY_SEGMENT_RE.match(p) for p in parts[1:])


def parent_keys(geo_key: str) -> list[str]:
    """Ancestor keys, outermost first: `id/{city}/{area}` → [`id`, `id/{city}`]."""
    parts = geo_key.strip("/").split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
