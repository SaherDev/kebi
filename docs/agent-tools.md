# The agent's tools — what each is for

Four tools after ADR-140. The question each one answers is different; where two
could answer the same question, one of them is gone.

| Tool | Cost per call | Answers | Fires when |
| ---- | ------------- | ------- | ---------- |
| `find_known` | free (2 indexed reads) | "what does kebi know near here that answers this?" | leads **every** recommendation turn |
| `find_saved` | free (one DB search) | "what does this user already have?" | any turn their own list could cover |
| `suggest_places` | **paid** (N provider lookups) | verifies the places *the orchestrator* names | coverage gap: new city, thin saves, no claims |
| `research` | free (claims read, maybe one geocode) | "what does a local know?" — no venue attached | pure knowledge questions |

## Verified coverage

Run live, one turn per case, fresh thread each (a reused thread answers from
conversation memory and calls nothing, which looks like a routing bug and is
not):

| Use case | Tools fired | Calls | Links |
| -------- | ----------- | ----- | ----- |
| night out, claims + saves | `find_known` → `find_saved` | 2 | 6 |
| errand with area claims | `find_known` → `suggest_places` | 2 | 4 |
| lunch now, thin claims | `find_known` → `suggest_places` | 2 | 4 |
| new city, no saves or claims | `find_known` → `find_saved` → `suggest_places` | 3 | 5 |
| knowledge question, no venue | `research` | 1 | 0 |

Every tool earns at least one case and no case exceeds 3 of the 5 allowed
calls. No further cut is warranted from this set.

## Why each survives

**`find_known` is the differentiator.** It is the only tool whose retrieval key
is a *fact*, so it is the only one that can say why one place beats another
tonight. It is also the cheapest. That combination is why it goes first.

**`find_saved` is a different corpus,** not a different ranking of the same
one. Nothing else can reach the user's own library.

**`suggest_places` verifies rather than invents.** Since ADR-141 the
orchestrator supplies the names from its own world knowledge and the tool only
checks each against the provider — no second naming model on the common path.
Verification cannot be skipped: a place with no catalog row has no id, so no
`kebi://venue/…` tap, and an unverified name may have closed years ago.

**`suggest_places` covers what kebi does not know yet.** It fired in neither
Canggu target answer, which makes it look droppable — and that reading is
wrong, because those came from a user with a dense save list in a
claims-covered area. Verified live: a user asking about Tokyo with no saves and
no claims gets `find_known` empty, `find_saved` empty, and then real Tokyo
venues from the namer, validated against the provider so the names exist and
the links resolve. Cutting it would look free on the Bali samples and silently
break every new city.

**`research` answers questions with no venue in them** ("do I tip here", "is it
safe at night") and can be scoped to an area the user names rather than the one
they are standing in. `find_known` is geofenced to the working location and
always returns places, so it cannot cover this.

## What was removed, and why it was the right one

`discover_places` (ADR-140). Its schema was byte-identical to
`suggest_places`, it cost the same per request, it fired zero times across the
captured answers, and its only job — return real nearby venues rather than let
the model invent a tip — was a *safety* property gated behind the model
remembering a conditional routing rule. It now runs automatically inside
`suggest_places` when nothing is named or validated, so the guarantee holds
without the routing.

## Token budget

| | tokens/request |
| --- | --- |
| `find_saved` | ~1,567 |
| `suggest_places` | ~1,567 |
| `research` | ~441 |
| `find_known` | ~253 |
| **total** | **~4,001** |

Against the ~5,397 baseline that included `discover_places`, that is a 26% cut.
It was 3,830 after ADR-140; ADR-141 added ~171 tokens back for the `names`
argument, buying the removal of an entire LLM call and its latency.

`find_saved` and `suggest_places` are expensive for the same reason: they share
the arg schema, and most of it is the controlled tag vocabulary moved there by
ADR-137. That is the next cut available — the vocabulary is serialised twice,
and only the dietary/accessibility classes actually filter.

## Where places come from

Nothing here searches the world for a place and stores it speculatively. A
catalog row exists because one of these put it there:

1. **Ingestion** — a shared video is extracted, each named place validated
   against the provider, and the row persisted with any claims harvested from
   the same content. This is how `BNI CANGGU` and `Bank Mandiri` exist: they
   came from the Bali ATM TikTok, each with one fee claim attached, which is
   what let `find_known` retrieve them for "atm without fees" later.
2. **`suggest_places` validation** — a name the namer proposed, confirmed to
   exist near the working location.
3. **The catalog floor** inside `suggest_places`, when neither of the above
   produced anything.

So the flywheel is ingestion: a place with claims is retrievable by what makes
it worth going to, and a place without them is just a name.
