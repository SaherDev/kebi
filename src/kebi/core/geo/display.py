"""Display-only cleanup of geocoder name dress. Never identity.

`strip_admin_unit` makes a *stored display string* read like a person wrote
it ("Khet Bang Rak" → "Bang Rak") on rows whose prose surfaces before a
registry row exists for them. It has zero effect on keys: identity is the
registry's, and the registry's own verification tolerates administrative
dress without these lists (`_names_agree`). A name this module doesn't
cover reads slightly bureaucratic somewhere — cosmetic, self-healing once
the registry's minted names take over — and corrupts nothing.
"""

from __future__ import annotations

from kebi.core.knowledge.schemas import _slugify

# Leading unit words some address languages prepend; trailing ones the
# English suffixes geocoders append. "Kota" and "City" are deliberately
# absent: they are part of real proper names ("Kota Kinabalu",
# "Ho Chi Minh City") — stripping them is the compound-name trap ADR-160
# warns about.
_LEADING_ADMIN_UNITS: frozenset[str] = frozenset(
    {
        "khet",
        "khwaeng",
        "tambon",
        "amphoe",
        "chang-wat",
        "changwat",
        "kecamatan",
        "kec",
        "kabupaten",
        "kab",
        "kelurahan",
        "thanh-pho",
        "quan",
        "huyen",
        "phuong",
        "thi-xa",
    }
)
_TRAILING_ADMIN_UNITS: frozenset[str] = frozenset(
    {"district", "regency", "subdistrict"}
)
# Trailing English suffixes strip only where they are the geocoder's
# translation of a local admin unit — in English-speaking countries
# "District" is part of the colloquial name itself ("Financial District")
# and must survive.
_TRAILING_STRIP_COUNTRIES: frozenset[str] = frozenset(
    {"id", "th", "vn", "la", "kh", "mm"}
)


def _strips_trailing(country_code: str | None) -> bool:
    return (
        country_code is not None
        and country_code.strip().lower() in _TRAILING_STRIP_COUNTRIES
    )


def strip_admin_unit(name: str, country_code: str | None = None) -> str:
    """Drop a leading/trailing administrative-unit word from a display name.

    "Khet Bang Rak" → "Bang Rak"; "Thành phố Huế" → "Huế"; "Ubud District" →
    "Ubud". Matching transliterates each word, so diacritic variants strip
    too; the surviving words keep their original spelling. A name that is
    only the unit word survives unchanged.
    """
    words = name.split()
    stripped = list(words)
    for n in (2, 1):
        if len(stripped) > n and _slugify(" ".join(stripped[:n])) in (
            _LEADING_ADMIN_UNITS
        ):
            stripped = stripped[n:]
            break
    if (
        _strips_trailing(country_code)
        and len(stripped) > 1
        and _slugify(stripped[-1]) in _TRAILING_ADMIN_UNITS
    ):
        stripped = stripped[:-1]
    return " ".join(stripped) if stripped != words else name
