# Model Cost Optimization — Problem & Goals

Status: **researched, not started**. Pick up from "Next steps".

## Problem

1. **Two model generations behind on the OpenAI fleet.** `extractor` and
   `area_registry` run GPT-4o ($2.50/$10 per 1M) and ~9 background roles run
   GPT-4o-mini ($0.15/$0.60) — both superseded by cheaper/better models
   (GPT-5.6 family, Gemini 3.x Flash, Chinese labs at a fraction of the price).
   (2026-08-20: `area_registry` was deliberately bumped mini → 4o — it fires
   once per unique area ever, so quality > cost there. Cost-irrelevant, but it
   should ride along to whatever replaces gpt-4o for the extractor.)
2. **No dollar visibility per role.** 15 logical roles in `config/app.yaml`, but
   no ranking of where the money actually goes. Decisions so far are by
   percentage, not dollars. Langfuse has the data; it hasn't been pulled.
3. **No control surface for the fleet.** Seven config blocks all say
   `openai / gpt-4o-mini` with slightly different params — changing "the cheap
   tier" means editing seven places. Feels like sprawl; is actually just
   config duplication.

## Goals

- Swap each role to the cheapest model that passes its eval — ranked by
  **dollar** savings (from Langfuse), not percentage.
- Fold the 10 background roles into shared tier definitions (YAML anchors) so a
  fleet swap is a one-line change. Zero runtime behavior change.
- Keep the orchestrator decision (69% of consult cost) evidence-gated: eval run
  before any move off Haiku 4.5.

## Verified pricing (2026-08-20, per 1M tokens in/out)

| Model | Price | Notes |
|---|---|---|
| Claude Haiku 4.5 (current orchestrator) | $1 / $5 | cache read $0.10 |
| Claude Sonnet 5 | **$2 / $10 permanent** (verified on Anthropic's page — not an intro rate) | strictly better than Sonnet 4.6 at $3/$15 |
| GPT-5.6 Terra / Luna | $2/$12 · $0.20/$1.20 | after July 30 price cut |
| Gemini 3.7 Flash | $0.75/$3.75 → **doubles to $1.50/$7.50 on 2027-01-01** | verified on Google's page; cache reads $0.075 + per-hour storage fee |
| Gemini 3.1 Flash-Lite | $0.25/$1.50 (vision included) | |
| Grok 4.1 Fast | $0.20/$0.50 | xAI is retiring "Fast" aliases → risky SKU |
| DeepSeek V4 Flash | **sources conflict**: $0.14/$0.28 vs $0.44/$1.33 | verify at provider console before committing |
| Qwen3.x Flash | ~$0.03/$0.13 | cheapest paid API anywhere |

Voyage 4-lite embeddings and Groq Whisper are already floor-priced — no action.

## Decisions reached

1. **Advanced orchestrator tier → Sonnet 5** (`orchestrator.advanced`). Permanent
   33% cut, same provider, caching unchanged. No-risk one-liner.
2. **Sonnet 5 as *default* orchestrator is now a live question** — at 2x Haiku
   (was 3x for Sonnet 4.6) the reliability upgrade may be affordable. Gate on
   Langfuse dollars: cheap orchestrator spend → just buy it; expensive → A/B via
   `AGENT_MODEL` (ADR-068) first.
3. **Orchestrator → Gemini: rejected.** Gemini bills cache storage per
   token-hour; with sporadic consult traffic the ~9k-token system prompt's
   storage fees eat the projected ~25–30% saving. Anthropic's 5-min ephemeral
   cache fits the traffic shape.
4. **Extractor → Gemini 3.7 Flash: right move, but NOT config-only** (see
   blockers). −70% now, −40% after the 2027 price doubling.
5. **Tier refactor via YAML anchors** in `app.yaml` (`_tiers:` + `<<: *cheap`)
   — folds 15 role decisions into ~3 tier decisions, no loader change needed.
6. **Batch API** halves OpenAI/Anthropic/Gemini pricing and all ~10 background
   roles qualify (all off the critical path) — but it's a submit-and-poll code
   path, not config-only. Only worth it if Langfuse shows real dollars there.
7. **Chinese labs** (DeepSeek/Qwen/GLM): aggressive option for background roles;
   blocked on new provider integration + unverified pricing + data-residency
   review. If tried, spike on one non-user-facing role (`web_harvester`) first.

## Blockers found in code (`src/kebi/providers/llm.py`)

The provider factory limits what a config-only swap can reach:

- `get_instructor_client` (extractor path): **openai/ollama only** — Gemini
  extractor needs an adapter (Gemini has an OpenAI-compatible endpoint, so
  possibly just a `base_url` branch + `GEMINI_API_KEY` secret; else
  `instructor.from_genai`).
- `get_langchain_chat_model` (orchestrator path): **anthropic/openai only** —
  Gemini orchestrator would need `langchain-google-genai`.
- `get_vision_extractor`: **openai only** — Gemini vision needs an adapter.
- Groq/Ollama already ride the OpenAI client via `base_url` — the same pattern
  is the cheapest route to any OpenAI-compatible provider (Gemini, DeepSeek,
  Qwen all offer compatible endpoints).

## Next steps (in order)

1. Pull 30-day per-model spend from Langfuse (needs `.env` creds — run
   manually):
   `curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" "$LANGFUSE_HOST/api/public/metrics/daily?limit=30"`
   → re-rank all of the below by dollars.
2. One-liner: `orchestrator.advanced` → `claude-sonnet-5`. Update `pricing:`
   block (in $2/$10; cache read $0.20, write-5m $2.50, write-1h $4.00) and
   Langfuse catalog; invalidate Redis LLM cache (CLAUDE.md rule).
3. Tier refactor (`_tiers:` anchors) — mechanical, zero behavior change.
4. Decide Sonnet-5-as-default using the Langfuse numbers (env A/B via
   `AGENT_MODEL` if spend is material).
5. Extractor → Gemini: add provider support (try OpenAI-compat `base_url`
   route), eval-harness run on structured extraction, then swap. Put the
   **2027 price** ($1.50/$7.50) in the `pricing:` block, not the intro rate.
6. Optional: vision → Gemini Flash-Lite (needs adapter + frame-OCR eval);
   background fleet → GPT-5.6 Luna or batch API (dollar-gated).

## Constraints checklist (per repo ADRs / CLAUDE.md)

- Every swap must be config-only (ADR-016); code adapters are their own tasks.
- `pricing:` block + Langfuse catalog move together with any swap
  (cost-reconciliation runbook).
- Redis LLM cache must be invalidated on model/prompt config change.
- `prompt_caching_enabled: true` is only valid for Anthropic orchestrators.
- Write an ADR when a task completes.
