"""Domain models for the knowledge layer's claims store (ADR-120).

The knowledge layer holds what exists in the world — country, city,
neighborhood, and place facts — regardless of where they came from. The
taste layer (`core.taste`, `core.user.intent_models`, `user_memories`) is
the separate counterpart: what *this user* cares about. The agent reads
both at query time; neither layer knows the agent's mode.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from anyascii import anyascii
from pydantic import BaseModel, ConfigDict

EntityType = Literal["country", "city", "neighborhood", "place"]
SourceType = Literal[
    "shared_content",
    "curated_expert",
    "kebi_message",
    "user_message",
    "web_search",
]
ReviewStatus = Literal["pending", "approved", "rejected"]

# Coarse, user-facing origin label for a surfaced claim (ADR-127). The raw
# `source_type` never crosses the wire; every reader (Library notes, research
# notes) maps through this one table so the labels can't diverge.
NOTE_SOURCE_LABELS: dict[SourceType, str] = {
    "shared_content": "community",
    "curated_expert": "expert",
    "kebi_message": "kebi",
    "user_message": "kebi",
    # Not "kebi" and not "community": a fact kebi read on a page is neither
    # something a person shared nor something kebi worked out. Labelling it
    # honestly is the point — the voice is allowed to be sure about a save
    # and hedged about a schedule it looked up.
    "web_search": "web",
}


def note_source_label(source_type: SourceType) -> str:
    """The coarse origin label for a claim's source_type."""
    return NOTE_SOURCE_LABELS.get(source_type, "community")


_COUNTRY_CODE_RE = re.compile(r"^[a-z]{2}$")


class KnowledgeClaim(BaseModel):
    """One claim about an entity, plus its provenance.

    Read/write boundary model — never the ORM row. `user_id` is None for a
    global claim (shared_content, curated_expert) and set for a
    conversation-origin claim (kebi_message, user_message), which is only
    ever read back for that same user.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    entity_type: EntityType
    entity_key: str
    entity_name: str
    claim: str
    tags: list[str] = []
    source_type: SourceType
    source_ref: str | None = None
    confidence: float
    user_id: str | None = None
    # Review gate (ADR-122). Defaults to approved (trust-everything today);
    # reviewed_by/reviewed_at are set only once an AI or team member reviews.
    review_status: ReviewStatus = "approved"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    # Corroboration tally (agree/disagree). Both 0 until the vote write-path
    # ships; carried through so the Library note can show the counts today.
    agree_count: int = 0
    disagree_count: int = 0
    created_at: datetime


class PlaceNote(BaseModel):
    """One insider note surfaced on a place — a claim reduced to what the
    Library read exposes (ADR-127).

    `id` is the underlying claim's id — a stable key for the client's list and
    the target the (future) agree/disagree vote will address. `agree_count` /
    `disagree_count` are its corroboration tally (0 until voting ships).
    `source_type` is kept internal so the API layer can map it to a coarse,
    user-facing label; it is not itself a wire field. `from_shared` is True
    when the underlying claim was harvested from the very post the user shared
    for this save (its `source_ref` equals the save's `source_ref`), so the
    client can badge "from what you shared" without any grouping server-side.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    tags: list[str] = []
    source_type: SourceType
    from_shared: bool = False
    agree_count: int = 0
    disagree_count: int = 0


class ResolvedGeo(BaseModel):
    """Geo an entity is anchored to, already resolved to canonical parts.

    `country_code` is an ISO-3166 alpha-2 code (what `build_geo_key`
    requires); `city`/`neighborhood` are display names the key builder
    slugifies. Harvest fills this from a place's stored location; curation
    fills it from a geocoder. A claim whose geo lacks the parts its scope
    needs is dropped by the writer rather than mis-keyed.
    """

    model_config = ConfigDict(frozen=True)

    country_code: str | None = None
    city: str | None = None
    neighborhood: str | None = None


class StructuredClaim(BaseModel):
    """A claim resolved to the point where an entity_key can be built.

    The shared hand-off both writers (harvest, curation) produce and the
    `KnowledgeWriter` consumes. The producing LLM never emits this directly
    with a key — its raw output is resolved (place index → catalog id, prose
    → geo) into this shape first, so a model can never invent a key.
    `confidence` is the model's own 0-1 estimate; the writer floors it by
    source trust. `scope` reuses `EntityType`.
    """

    model_config = ConfigDict(frozen=True)

    scope: EntityType
    entity_name: str
    claim: str
    tags: list[str] = []
    confidence: float
    place_ref: str | None = None
    geo: ResolvedGeo | None = None


class CurationAnchor(BaseModel):
    """The entity a curation request is pinned to, resolved before the LLM
    runs (never by it). `place_id` is set for a venue anchor and None for an
    area anchor; `geo` is the fallback geography for the prose's geo-scoped
    claims either way — a venue's stored location, or the area's own key
    parts. Slug-built geo is fine: `_slugify` is idempotent, so an already-
    canonical slug keys identically to the display name it came from."""

    model_config = ConfigDict(frozen=True)

    place_id: str | None = None
    name: str
    geo: ResolvedGeo


class WrittenClaim(BaseModel):
    """A claim that produced a new row, paired with that row's id — what the
    write path returns so a caller can reference (and later retract) exactly
    what it just created. Dedup-collapsed resubmissions have no entry: their
    row (and id) predates this write."""

    model_config = ConfigDict(frozen=True)

    id: str
    claim: StructuredClaim


class HarvestPlace(BaseModel):
    """A place identified during extraction, with its resolved geo, that the
    harvester reasons about. `place_id` is the catalog id used to key
    place-scoped claims; `geo` anchors any geo-scoped claim about it."""

    model_config = ConfigDict(frozen=True)

    place_id: str
    name: str
    geo: ResolvedGeo


class HarvestContent(BaseModel):
    """The already-gathered content of one share, snapshotted for the
    second pass. No raw media — only the derived text extraction already
    produced (ADR: reuse content, no re-fetch)."""

    model_config = ConfigDict(frozen=True)

    caption: str | None = None
    transcript: str | None = None
    hashtags: list[str] = []
    title: str | None = None
    platform: str | None = None
    location_tag: str | None = None
    source_ref: str | None = None

    def is_empty(self) -> bool:
        """True when there is nothing worth harvesting from."""
        return not any(
            (
                self.caption,
                self.transcript,
                self.title,
                self.location_tag,
                self.hashtags,
            )
        )


class HarvestSnapshot(BaseModel):
    """The full bucket payload for one harvestable extraction event —
    content plus the places it produced. Written to object storage under a
    `harvest/{request_id}.json` key; the harvest event carries only the key,
    and the handler reads this back."""

    model_config = ConfigDict(frozen=True)

    content: HarvestContent
    places: list[HarvestPlace] = []


def _slugify(part: str) -> str:
    """Diacritic- and script-insensitive slug so one place keys the same
    regardless of how its name is written.

    `anyascii` transliterates any script to ASCII first, so a name in its local
    script and its romanised spelling collapse to one stable key: "Hội An" and
    "Hoi An" → "hoi-an"; "Đà Nẵng" and "Da Nang" → "da-nang"; "東京" →
    "dongjing"; "กรุงเทพ" → "krungethph". The ASCII result is lowercased and
    every run of non-alphanumeric characters becomes a single hyphen.
    """
    out: list[str] = []
    prev_hyphen = False
    for ch in anyascii(part).lower():
        if ch.isalnum():
            out.append(ch)
            prev_hyphen = False
        elif not prev_hyphen:
            out.append("-")
            prev_hyphen = True
    return "".join(out).strip("-")


def build_place_key(place_id: str) -> str:
    """Canonical entity_key for a place: its catalog id, namespaced."""
    return f"place:{place_id}"


# Administrative-unit affixes folded out of area names before slugging and
# stripped from stored display geo, so "Khet Bang Rak" and "Bang Rak" key and
# read the same (ADR-163 — the neighborhood-level treatment ADR-153 asked
# for). Leading affixes are the unit words Thai/Indonesian/Vietnamese
# addresses prepend; trailing ones the English suffixes geocoders append.
# "Kota" and "City" are deliberately absent: they are part of real proper
# names ("Kota Kinabalu", "Ho Chi Minh City") — stripping them is the
# compound-name trap ADR-160 warns about.
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
# Trailing English suffixes strip only where they are Google's translation
# of a local admin unit ("Ubud District", "Karangasem Regency") — in
# English-speaking countries "District" is part of the colloquial name
# itself ("Financial District", "Mission District") and must survive.
_TRAILING_STRIP_COUNTRIES: frozenset[str] = frozenset(
    {"id", "th", "vn", "la", "kh", "mm"}
)


def _strips_trailing(country_code: str | None) -> bool:
    return (
        country_code is not None
        and country_code.strip().lower() in _TRAILING_STRIP_COUNTRIES
    )


# Areas the geocoder names by administrative village where people use one
# colloquial name for the whole stretch — the neighborhood-level counterpart
# of _CITY_ALIASES (ADR-163, the treatment ADR-153 asked for). Keyed by
# (country_code, folded slug of the variant) → canonical slug. Same
# maintenance cost and same accepted trade-off as the city table: a pair
# nobody has added still splits silently.
_AREA_ALIASES: dict[tuple[str, str], str] = {
    # Canggu colloquially spans the Tibubeneng desa (Berawa) next door.
    ("id", "tibubeneng"): "canggu",
    # The Uluwatu area's venues sit in Pecatu village on Google's map.
    ("id", "pecatu"): "uluwatu",
    # Gili Indah is the desa covering all three Gilis; Google names a place
    # by it on some rows and by the island on others, splitting one island
    # across two areas. Folded to the island people actually say. Accepted
    # imprecision, the same kind Tibubeneng→Canggu carries: a save on Gili
    # Air or Gili Meno that Google names only by the desa reads as Gili
    # Trawangan, and only coordinates could tell them apart.
    ("id", "gili-indah"): "gili-trawangan",
    # Endonym/exonym pair Google returns in both slots — as the area of a
    # save in Boom, and as the city of one in Antwerp itself. Folded here
    # rather than in _CITY_ALIASES because only this table reaches the
    # neighborhood segment, and canonical_city_slug folds through here on
    # its way to the city lookup, so one entry canonicalises both.
    ("be", "antwerpen"): "antwerp",
}


def canonical_area_slug(slug: str, country_code: str | None = None) -> str:
    """Fold an already-slugged area name's admin-unit affixes away.

    "khet-khlong-toei" → "khlong-toei"; "thanh-pho-hue" → "hue"; with a
    trailing-strip country, "ko-samui-district" → "ko-samui". A slug that IS
    the unit word alone is left as-is — stripping must never empty a key
    part.
    """
    for token in sorted(_LEADING_ADMIN_UNITS, key=len, reverse=True):
        prefix = f"{token}-"
        if slug.startswith(prefix) and len(slug) > len(prefix):
            slug = slug[len(prefix) :]
            break
    if _strips_trailing(country_code):
        for token in _TRAILING_ADMIN_UNITS:
            suffix = f"-{token}"
            if slug.endswith(suffix) and len(slug) > len(suffix):
                slug = slug.removesuffix(suffix)
                break
    if country_code is not None:
        slug = _AREA_ALIASES.get((country_code.strip().lower(), slug), slug)
    return slug


def strip_admin_unit(name: str, country_code: str | None = None) -> str:
    """Drop a leading/trailing administrative-unit word from a display name.

    "Khet Bang Rak" → "Bang Rak"; "Thành phố Huế" → "Huế"; "Ubud District" →
    "Ubud" (trailing only in countries where the suffix is a translation,
    never where it's the name — "Financial District"). Matching
    transliterates each word, so diacritic variants strip too; the surviving
    words keep their original spelling. A name that is only the unit word
    survives unchanged.
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


# Cities the geocoder names inconsistently between calls — an English exonym
# on one lookup, the local endonym on the next. `_slugify` cannot merge these:
# "Bangkok" and "Krung Thep Maha Nakhon" are different words, not
# transliterations of one another, so the slug is correctly different and the
# claims split across two keys that no prefix scan will ever join (ADR-144).
#
# Keyed by (country_code, slug of the variant) → canonical slug. This is a
# maintained list, which is a real cost: a pair nobody has added still splits
# silently. It is the pragmatic half of the fix — keying cities by a stable
# geocoder id would remove the maintenance entirely, and is the direction to
# take when a claim volume justifies the migration.
_CITY_ALIASES: dict[tuple[str, str], str] = {
    ("th", "krung-thep-maha-nakhon"): "bangkok",
    ("th", "krung-thep"): "bangkok",
    ("id", "jakarta-raya"): "jakarta",
    ("id", "daerah-khusus-ibukota-jakarta"): "jakarta",
    # Locality-less provinces Google names in Indonesian on one fetch and in
    # English on the next; the English form is canonical so the two Nusa
    # Tenggaras key alike. Both directions observed live on saved places.
    ("id", "nusa-tenggara-barat"): "west-nusa-tenggara",
    ("id", "nusa-tenggara-timur"): "east-nusa-tenggara",
    ("vn", "thanh-pho-ho-chi-minh"): "ho-chi-minh-city",
    ("vn", "sai-gon"): "ho-chi-minh-city",
    ("vn", "ha-noi"): "hanoi",
    ("vn", "da-nang"): "da-nang",
    ("jp", "tokyo-to"): "tokyo",
    ("kr", "seoul-teukbyeolsi"): "seoul",
    ("cn", "beijing-shi"): "beijing",
    ("ae", "dubayy"): "dubai",
}


def canonical_city_slug(country_code: str, city: str) -> str:
    """The slug a city keys under, folding known name variants together.

    Applied at the one place keys are built, so a claim written from a
    geocode that said "Krung Thep Maha Nakhon" lands under the same key as
    one that said "Bangkok". Admin-unit affixes fold before the second alias
    lookup ("Thành phố Huế" → "hue") so official municipality styles cannot
    split a city either. Unknown cities pass through unchanged.
    """
    cc = country_code.strip().lower()
    slug = _slugify(city)
    if (cc, slug) in _CITY_ALIASES:
        return _CITY_ALIASES[(cc, slug)]
    folded = canonical_area_slug(slug, cc)
    return _CITY_ALIASES.get((cc, folded), folded)


def build_geo_key(
    country: str, city: str | None = None, neighborhood: str | None = None
) -> str:
    """Canonical entity_key for a geo entity: a lowercased hierarchical slug.

    `country` must be an ISO-3166 alpha-2 code (e.g. "ae"). A neighborhood
    key requires a city; "all claims under Dubai" is a prefix scan on the
    city-level key this returns for a city alone.

    The city component is canonicalised (ADR-144) so name variants for one
    city cannot split its claims across keys a prefix scan will never join;
    the neighborhood component folds admin-unit affixes the same way
    (ADR-163) so "Khet Khlong Toei" and "Khlong Toei" key identically.
    """
    country_code = country.strip().lower()
    if not _COUNTRY_CODE_RE.match(country_code):
        raise ValueError(f"country must be an ISO-3166 alpha-2 code, got {country!r}")
    if neighborhood is not None and city is None:
        raise ValueError("neighborhood key requires a city")

    parts = [country_code]
    if city is not None:
        parts.append(canonical_city_slug(country_code, city))
    if neighborhood is not None:
        parts.append(canonical_area_slug(_slugify(neighborhood), country_code))
    return "/".join(parts)
