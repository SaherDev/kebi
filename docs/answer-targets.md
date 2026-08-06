# Answer targets

What a Kebi answer has to look like. These are acceptance targets, not
examples of tone — each one is here because it fails in a specific way when a
layer is missing, and the failure is the thing to test for.

The rule behind all of them (ADR-137): **strip the Kebi-specific layers out of
an answer and it must stop making sense.** If what's left still reads fine,
the answer was the free generic plan wearing place names.

Related: ADR-136 (chat renders text + `kebi://` links only), ADR-137 (claims
and saves layered onto every answer), ADR-138 (claims-driven retrieval).

---

## Target 1 — the night out

**Ask:** "where should i go tonight" (Monday, in Canggu)

**Target answer:**

> its monday so tonight is Luigis night, thats the one from your video, monday
> is their big night in canggu, gets packed after 11 so go 10ish if you want a
> table first
>
> the rest of your saved list has a schedule btw
>
> - friday is Finns, also from your video, day club so thats an afternoon into
>   sunset move not a night one
> - Savaya is a sunset cliff club, best weekends, book a daybed ahead or you
>   stand
> - Motel Mexicola works any night, dinner into party, no day rule, good backup
>   when the big nights dont line up
>
> so tonight luigis, and your friday is already sorted

**What each line requires:**

| Line | Requires |
| ---- | -------- |
| "tonight is Luigis night" | A claim (Luigi's = Monday) **retrieving** the place, not annotating it — `find_known`. Ranking nearby nightclubs never produces this. |
| "thats the one from your video" | The save's provenance (`source`/`source_ref`), used as a reason rather than dropped |
| "gets packed after 11 so go 10ish" | A per-place claim spent as advice, with the timing inference the user actually needs |
| "the rest of your saved list has a schedule" | Answering the *week*, not just the question asked — the schedule across saves is itself the insight |
| "day club so thats an afternoon move not a night one" | A claim contradicting the ask, surfaced honestly instead of padding the list |
| "good backup when the big nights dont line up" | Ordering with a stated role per pick, not a ranked dump |
| Every place name tappable | `kebi://venue/{id}` links attached server-side (ADR-136) |

**Fails if:** it names nightclubs with no day-of-week reasoning; it lists saves
without pinning each to a day; it recommends a day club for tonight.

---

## Target 2 — the errand where the brand matters

**Ask:** "where's the nearest ATM?" (in Bali)

**Target answer shape:** the fee knowledge picks the bank, then the search
finds that bank's nearest branch — both halves, in that order.

> use BNI or Maybank — neither charges a withdrawal fee here, and Maybank lets
> you take out 3 million at once. closest BNI is the one on \<street>. avoid
> Mandiri, thats 55k a pull.

**What it requires:**

| Element | Requires |
| ------- | -------- |
| "BNI or Maybank, no fee" | Claims retrieved for the *topic* ("ATM without fees"), not for a category |
| "3 million at once" | A specific claim spent, not generalised away |
| "closest BNI is …" | A **second** call searching for that named brand — the fact chooses *what*, the place search finds *where* |
| "avoid Mandiri, 55k" | A negative claim used as a warning |

**Fails if:** it returns the geometrically nearest ATM with no fee reasoning;
it repeats the fee tip without finding a branch; it names a bank kebi holds no
claim about.

---

## Standing requirements (every answer)

1. **Layers, in order** — the plan; saves pinned to it; insider claims per
   place and area; taste picks where nothing is saved.
2. **Voice** — a local friend. Honest about gaps ("no idea about their
   kitchen") rather than filling them.
3. **Links** — place and area names are plain prose in the model's output and
   arrive at the client as `kebi://venue/{id}` / `kebi://area/{key}` taps. No
   cards, no per-tool payloads.
4. **Retrieval along the way** — a route turn ("on the way to X") searches the
   whole route, not a circle around where the trip starts.
5. **Nothing invented** — every place name comes from a tool result, every
   insider claim from `notes` / `area_notes` / `research`.

---

## How to run these

Ingest the source videos, then ask. The Canggu nightlife set and the Bali ATM
set both come from shared TikToks; extraction writes the claims that make the
targets reachable.

```bash
# 1. ingest (per video)
curl -X POST localhost:8077/v1/extract \
  -H "Content-Type: application/json" \
  -H "X-Gateway-Token: $GATEWAY_SHARED_SECRET" \
  -H "X-Gateway-User-Id: user_2abcdefghijklmnopqrstuvwx" \
  -H "X-Gateway-Discovery-Enabled: true" -H "X-Gateway-Taste-Enabled: true" \
  -H "X-Gateway-Save-Limit: 500" -H "X-Gateway-Consults-Per-Day: 500" \
  -d '{"raw_input":"<tiktok url>"}'

# 2. ask
curl -X POST localhost:8077/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-Gateway-Token: $GATEWAY_SHARED_SECRET" \
  -H "X-Gateway-User-Id: user_2abcdefghijklmnopqrstuvwx" \
  -H "X-Gateway-Discovery-Enabled: true" -H "X-Gateway-Taste-Enabled: true" \
  -H "X-Gateway-Save-Limit: 500" -H "X-Gateway-Consults-Per-Day: 500" \
  -d '{"message":"im in canggu bali, its monday - where should i go tonight?"}'
```

Check the claims actually landed before blaming the answer:

```sql
select entity_type, entity_name, claim, tags from knowledge_claims
order by created_at desc limit 20;
```

A missing target line is usually a missing claim, not a missing prompt rule —
diagnose ingestion first.

---

## Fixed — area keys are canonicalised (ADR-144)

City slugs now fold known name variants together at `build_geo_key`, and a
migration repaired the rows written before that. The section below is kept as
the record of what the failure looked like and what remains partial: the alias
table is maintained by hand, so an unlisted city pair still splits silently.

## Known issue (now partially addressed) — area keys were not stable

`kebi://area/{key}` is well-formed and byte-identical to the knowledge layer's
`entity_key`, so a tap resolves and prefix scans work. But the key is built by
slugifying whatever **display name** the geocoder returned for the city, so the
same real area can produce more than one key. Evidence from the live store:

```
city          th/bangkok                                    Bangkok
neighborhood  th/krung-thep-maha-nakhon/khet-khlong-toei    Khet Khlong Toei
```

Same city, two keys — English name and Thai name. A prefix scan on
`th/bangkok` never finds that neighborhood's claims. The same shape shows up
as `id/bali/canggu` vs `id/badung/canggu` (province vs regency, depending on
the geocode response).

Consequences: claims for one place accumulate under two keys, so the
ingestion flywheel leaks; and the client can get two different links for the
same area with no way to dedupe them.

This predates ADR-136 (it is how `build_geo_key` has always worked, ADR-120 /
ADR-126), but ADR-136 raised the stakes by making area keys user-facing tap
targets rather than an internal index.

**Proposed fix, not yet done:** stop deriving the key from a display name.
Resolve the city to a stable geocoder identity (provider place id) and carry
the display name alongside, so name variants collapse to one key. A cheaper
interim is a committed variant→canonical alias map applied at the single
`build_geo_key` choke point. Either needs a migration for existing rows, which
is why it is logged here rather than bolted onto the ADR-136/137/138 work.

Separately, one row is mis-scoped: `country | vn | "Ha Giang loop"` — a trek
route stored as a country. That is a harvest-scoping bug, not a key bug.
