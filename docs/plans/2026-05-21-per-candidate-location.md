# Per-candidate location for multi-destination posts (ADR-082)

## Context

ADR-080 biases every place search by **one shared location** inferred
for the whole post. That breaks on a multi-destination travel listicle.

Observed bug — a TikTok "3 must-visit places around Amsterdam" listicle
(`@katharinafriedl_/video/7431909152563203361`) spans three towns:

```
1. Zaanse Schans   2. Zaandam (→ Inntel Hotel)   3. Volendam (→ Doolhof)
```

The resolver inferred the single location "Amsterdam" and biased every
search to it. "Inntel Hotel" — explicitly under the *Zaandam* section —
searched as `"Inntel Hotel Amsterdam"` and resolved to **Inntel Hotels
Amsterdam Centre** instead of the Zaandam Inntel. The post's own section
structure named the right town; the single-location model discarded it.

## Fix

Keep the shared post location as the default; add a **per-candidate
`area`** override. A venue the post places in a specific town is
searched biased by *that* town, not the post-wide region. A section
header that is itself a town/city/region becomes the `area` for the
venues under it and is **not** saved as a place; a header that is a
specific attraction (e.g. Zaanse Schans) is both saved and used as the
area. The resolver derives `area` from explicit text, listicle section
structure, or the model's own geographic knowledge — knowledge informs
the *search bias* only; the provider stays the source of identity
(ADR-070). Single-location posts carry no `area` and behave exactly as
before — a strict superset of ADR-080.

## Changes

| File | Change |
|---|---|
| `core/extraction/enrichers/llm_resolver.py` | `area` field on `_ResolvedCandidate` / `_DiscoveredCandidate`; `resolve()` builds `query_locations` (per-candidate `LocationContext`, country inherited from the shared location); `_area_location` helper |
| `core/extraction/candidate_mapper.py` | `ResolverOutput.query_locations` — sparse map of per-candidate location overrides |
| `core/extraction/extraction_pipeline.py` | `_extend_search_set` biases each `find()` by `query_locations.get(name)`, falling back to the shared location |
| `config/prompts/place_resolver.txt` | section 2 rewritten "Locate the post" (shared `location` + per-candidate `area` + world knowledge); section 1 gains the `area` field + the "towns are not candidates" rule |
| `config/prompts/place_classifier.txt` | same-name disambiguation: a venue's own section/area overrides post-wide location signals |
| `core/extraction/enrichers/llm_picker.py` | corrected stale `gpt-4o-mini` tracing label (the `extractor` role is `gpt-4o`) |
| `docs/decisions.md` | ADR-082 |

No model change: the resolver/picker already run on `gpt-4o` (the
`extractor` role) — only the stale tracing labels were wrong.

## Verification

```bash
poetry run pytest tests/core/extraction/      # 225 passed, 1 skipped
poetry run ruff check src/ tests/
poetry run mypy src/kebi/core/extraction/
```

New tests: `test_llm_resolver.py` — per-candidate `area` →
`query_locations` (resolved + discovered candidates); pipeline —
`test_per_candidate_location_biases_each_search` asserts each `find()`
gets its candidate's own location and others fall back to the shared
one.

End-to-end: re-run `POST /v1/extract` on the listicle URL (clear the
`extract:v1:*` cache entry first) — "Inntel Hotel" must resolve to the
Zaandam Inntel; Zaandam/Volendam must not be saved as standalone places.

Pre-existing `places_v2` `REGCONFIG` mypy failure
(`embeddings_repo.py:133`) is unrelated and out of scope.
