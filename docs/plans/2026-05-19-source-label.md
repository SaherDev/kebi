# Plan: Per-save source label + gated global aliases (ADR-081) — SHIPPED

## Problem

A place discovered in a post under a familiar label ("Mirror
Temple") is saved/shown under the canonical provider name ("Wat
Phuttha Prommayan"), forcing the user to re-identify their own saves.
The as-seen label lived only transiently in `evidence[].snippet` and
was never persisted. Separately, the shared `place_name_aliases`
field is fully consumed by search (folded into the indexed text via
`embedding_service`) but was never populated by extraction.

## What shipped (ADR-081)

- **Per-user `source_label`** on `user_places` (new nullable column,
  migration `c3d4e5f6a7b8`, down_revision `b2c3d4e5f6a7`). Set to the
  raw on-screen label (`AttributedSearchResult.query`, reused as-is)
  when it differs (normalised) from the canonical `place_name`; else
  NULL. **Ungated** — it is the user's own memory.
- **Gated global `place_name_aliases`**: the same label is added to
  the shared catalog field **only** when
  `confidence >= extraction.confidence.confident_threshold` (0.70,
  reused — no new knob) AND it differs from canonical. Protects
  shared search from wrong-match poisoning (the #8 "Park Sathorn"
  case stays per-user-only).
- **Contract**: `ExtractPlaceItem.source_label: str | None`. Result
  cache round-trips it for free (Pydantic). Product chooses which
  name to headline; this repo returns both.

## Touch points

- `alembic/versions/c3d4e5f6a7b8_add_user_places_source_label.py`
- `core/places/user_places_repo.py` (`_UserPlacesTable`, dict/row helpers)
- `core/places/models.py` (`UserPlace.source_label`)
- `core/places/user_places_service.py`, `protocols.py` (`save_places(source_labels=)`)
- `core/extraction/types.py` (`ValidatedCandidate.source_label`)
- `core/extraction/candidate_mapper.py` (`reconcile_picks` sets it;
  `candidate_to_core(aliases=)`)
- `core/extraction/service.py` (gated alias build; per-user
  `source_labels` map; `_link_to_user`/`_candidate_to_item_dict`)
- `api/schemas/extract_place.py` (`ExtractPlaceItem.source_label`)
- `docs/decisions.md` (ADR-081), `docs/api-contract.md`

Out of scope (future): resolver-cleaned `display_label` for OCR
garble; back-fill of existing rows.

## Verification

`pytest`, `ruff check`, `mypy src/`, alembic up/down reversible, and a
fresh `POST /v1/extract` on the thailandtale 10-place URL: Mirror
Temple item carries `source_label:"Mirror Temple"`,
`place.place_name:"Wat Phuttha Prommayan"`, and (confidence ≥ 0.70) a
`place_name_aliases` entry; a below-threshold pick gets the per-user
label but no global alias.
