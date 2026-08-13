"""Web URL codec — one routable path segment for a source URL.

A web source link is `kebi://web/{token}` where the token encodes the full
page URL. Encoding keeps the scheme uniform with venues and areas — one
opaque segment, no client-side URL parsing rules — and mirrors the area
codec (ADR-153): reversible on purpose, so a token *is* its URL and no
lookup table exists to drift or miss.

Unlike area tokens, nothing server-side resolves these: the client decodes
the token locally (documented in the API contract) and opens the page.
Kebi only ever mints tokens for URLs it fetched itself this turn, so a
token can never point at a page the answer did not read.
"""

from __future__ import annotations

import base64
import binascii

# Only web pages are ever minted. Anything else (mailto:, javascript:, a bare
# domain) is a bug upstream, and refusing to encode it here keeps a malformed
# link from reaching a client that will try to open it.
_ALLOWED_PREFIXES = ("https://", "http://")


def encode_web_url(url: str) -> str:
    """URL-safe token for a fetched page URL.

    Raises ValueError on anything that is not an http(s) URL — a token must
    never be minted for a value the decoder would hand a client to open.
    """
    if not url.startswith(_ALLOWED_PREFIXES):
        raise ValueError(f"not a web url: {url!r}")
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_web_url(token: str) -> str:
    """The page URL a token encodes.

    Raises ValueError when the token is not one this codec minted — wrong
    alphabet, or a payload that is not an http(s) URL.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        url = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"not a web token: {token!r}") from exc
    if not url.startswith(_ALLOWED_PREFIXES):
        raise ValueError(f"not a web token: {token!r}")
    return url
