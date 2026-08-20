# Model benchmark — round 1 (2026-08-20, ADR-175)

Harness: `poetry run python -m kebi.eval.bakeoff --role <role> --options ...`
Golden sets: `config/evals/golden/<role>/` (seed sets; expand from real
traces with `scripts/export_golden_traces.py`). Re-run after any golden-set
change — numbers below are for the seed sets (8–12 cases) and are
directional, not final.

## extractor (gpt-4o today, 2 calls per uncached share)

| option | model | quality | pass | p50 ms | p95 ms | $/1k cases |
|---|---|---|---|---|---|---|
| current | gpt-4o | 0.875 | 88% | 1365 | 2718 | 5.15 |
| luna | gpt-5.6-luna (effort none) | 0.875 | 88% | 2079 | 12862 | 0.33 |

**Read:** Luna ties gpt-4o on quality at ~1/16th the cost. p95 latency is
the concern — `/v1/extract` is synchronous — so the swap trial should
watch extraction latency, not quality. Note: gpt-5.6-luna requires
`reasoning_effort: none` for function tools on chat completions (staged in
its config option after the first bakeoff run failed all 8 cases without it).

Pending options (need `OPENROUTER_API_KEY`): gemini-flash, qwen-plus,
deepseek-flash.

## location_resolver (claude-haiku-4-5 today, fires every located turn)

| option | model | quality | pass | p50 ms | p95 ms | $/1k cases |
|---|---|---|---|---|---|---|
| current | claude-haiku-4-5 | 0.917 | 75% | 1787 | 3815 | 6.89* |
| luna | gpt-5.6-luna (effort none) | 0.833 | 58% | 2197 | 3513 | 0.36 |

**Read:** Haiku holds a real quality lead on the resolver's nuanced rules
(shift-vs-continuation, stated modes, clarification) — consistent with the
live failures that moved this role off gpt-4o-mini originally. Do NOT swap
on price alone. \*Production resolver cost is now far below the table's
uncached number: ADR-174's cache split cut live per-turn cost ~65%.

Pending options: gemini-flash, qwen-flash (OpenRouter).

## orchestrator

Not benchmarked this round — needs the bound tool loop + LLM judge; its
adapter is built when its swap round starts (last, per plan). ADR-100's
acceptance gates remain the production quality bar.

## Background mini-roles (pricing-only review)

All run gpt-4o-mini ($0.15/$0.60 per 1M). Current market:

| candidate | $/1M in/out | vs mini |
|---|---|---|
| gpt-4o-mini (incumbent) | 0.15 / 0.60 | — |
| gpt-5.6-luna | 0.20 / 1.20 | costlier per token, 2 generations newer |
| qwen3.5-flash (OpenRouter) | 0.065 / 0.26 | ~2.3x cheaper |
| gemini-3.7-flash | 0.75 / 3.75 (intro) | costlier |

**Read:** mini is already near the price floor and these roles are one-shot
or debounced background work — their combined spend is small (see
`scripts/llm_cost_report.py`). There is no cost case for bulk-swapping
them; revisit per-role only if a quality gap shows up (e.g. a profiler
missing facts) — then Luna is the quality upgrade, qwen-flash the cost cut.

## Swap order (Task 5)

1. `extractor` → luna trial (`KEBI_MODEL_EXTRACTOR=luna`), watch
   `/v1/extract` latency + validation-retry rate for 2–3 days.
2. `location_resolver` — stays on Haiku unless a candidate beats 0.917 on
   an expanded golden set.
3. `orchestrator` — last, behind its own adapter + ADR-100 gates.
