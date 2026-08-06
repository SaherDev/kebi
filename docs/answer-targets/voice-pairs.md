# Voice pairs — before and after

Two real agent answers alongside the same answer in the target register. The
"before" side is captured output (`target-1-night-out.json`,
`target-2-atm-fees.json`); the "after" side is what shipped into the voice
block of `config/prompts/agent.txt` as the worked examples.

The point of the pair is that **only the skin changes.** Same picks, same
claims, same ordering, same skips, near-identical length. Composition was
already right in both samples; register was the failure. Keep them paired so a
future prompt change can be checked against both axes independently.

## What the diff taught (now prompt rules)

| Before | After | Rule |
| ------ | ----- | ---- |
| em dashes throughout | commas, full stops, "so" | no em/en dashes |
| `**Bold headers:**` | plain lead-in line + short bullets | no bold, no markdown headers |
| Sentence-case openings | lowercase, mirroring the user | mirror the user's register |
| "Luigi's Hot Pizza Canggu" | "Luigi's" | say it the way a person says it |
| "Motel Mexicola \| Canggu" | "Motel Mexicola" | never a raw provider string |
| "ATM BNI Pererenan Gas Station" | "ATM BNI Pererenan at the gas station" | location detail in prose, not in the name |
| "Luigi's → Old Man's" | "luigis then old mans" | words, not symbols |
| `Canggu` linked to the area inside "Jl. Raya Canggu" | the venue's own key | link the thing you actually mean |

## Coverage

| Intent class | Pair |
| ------------ | ---- |
| Night planning | night-out (Canggu, Monday) |
| Utility errand | ATM fees (Bali) |
| Route / on-the-way | **missing** — capture the first real route answer as the "before" |

A route pair would complete the set. The corridor retrieval it depends on
landed with ADR-138 (`anchor_to_corridor`), so the case is reachable; it just
has not been captured yet.

## Structure follows the question

The night out earned a skip list because a day fact justified every exclusion.
The errand carries everything in three plain paragraphs. Neither shape is a
template — a skip list with no fact behind each line is an inventory report,
which is why "skips need a reason" is a rule rather than a style note.
