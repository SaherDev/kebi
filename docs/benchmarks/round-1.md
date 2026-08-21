# Model benchmark — round 1 (2026-08-20/21, ADR-175)

## Round 1b — OpenRouter matrix (2026-08-21, ADR-181)

Run after the OpenRouter key landed. Note: the account's free allowance
ran out mid-race — qwen's orchestrator leg is unraced and one MiniMax
case 402'd; add a few dollars of credits to finish.

**extractor** (current = gpt-5.6-luna after ADR-178):

| option | quality | pass | p50 ms | $/1k |
|---|---|---|---|---|
| deepseek-flash (v4) | **0.958** | 88% | 7292 | 0.65 |
| current (luna) | 0.875 | 88% | 1857 | 0.32 |
| qwen-plus | 0.708 | 62% | 3709 | 2.06 |
| gemini-flash | unusable | — | — | — |

Read: DeepSeek V4 Flash actually out-scored Luna — but at 4x the p50 on a
synchronous endpoint. Luna keeps the role; DeepSeek is the named
challenger if extraction ever goes async. Gemini 3.7 Flash cannot run
this role at all: its reasoning is mandatory and eats the 512-token
structured-output budget.

**location_resolver** (current = claude-haiku-4-5):

| option | quality | pass | p50 ms | $/1k |
|---|---|---|---|---|
| current (haiku) | 0.833* | 67% | 2382 | 6.91 |
| qwen-flash (effort none) | 0.861 | 67% | 1507 | 0.40 |
| gemini-flash | 0.611 | 42% | 6602 | 6.47 |

\*Haiku scored 0.917 on 2026-08-20; today's run had one transient
tool-parse error. Read: **qwen3.5-flash is statistically tied with Haiku
at ~1/17th the cost** — the first credible resolver challenger. Expand
the golden set (30+ cases from real traces) before deciding; do not swap
on a 12-case tie. Gemini loses outright.

**orchestrator routing** (first-move test, ADR-180):

| option | quality | pass |
|---|---|---|
| sonnet-5 / sonnet-4.6 / luna | 1.000 | 100% |
| gemini-flash | 0.917 | 92% (missed practical-errand) |
| minimax-m3 | 0.917 | 92% (1 credit-402) |
| current (haiku) | 0.917 | 92% (missed vegan constraint) |
| qwen-flash | unraced | credits ran out |

**Provider quirks found this round** (all now config fields):
`max_tokens`/`temperature` were historically NEVER sent on the Instructor
path (config values were decorative — now enforced); the GPT-5.6 family
requires `max_completion_tokens` on OpenAI-direct; Qwen thinking mode
rejects forced tool_choice (fixed with `reasoning_effort: none`); Gemini
3.7 Flash reasoning cannot be disabled; OpenRouter qwen ids carry date
suffixes.


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

## orchestrator — routing-strength test (added 2026-08-21, ADR-180)

First move on 12 routing cases: correct tool (or direct answer) + hard
constraints carried in args. Tools bound with real schemas, nothing
executes; multi-step loop quality and prose voice are NOT measured yet.

| option | model | quality | pass | p50 ms | p95 ms | $/1k cases |
|---|---|---|---|---|---|---|
| sonnet-5 | claude-sonnet-5 (no temp) | 1.000 | 100% | 7746 | 11883 | 36.68 |
| sonnet-strong | claude-sonnet-4-6 | 1.000 | 100% | 3109 | 6555 | 41.41 |
| luna-workhorse | gpt-5.6-luna | 1.000 | 100% | 2547 | 11299 | 2.10 |
| current | claude-haiku-4-5 | 0.917 | 92% | 1584 | 2875 | 13.83 |

**Reads:**
- **The harness caught a prod-breaking quirk:** Sonnet 5 rejects the
  `temperature` parameter (400) — the ADR-177 advanced-tier swap would
  have failed every advanced consult on deploy. Now a profile flag
  (`supports_temperature: false`); clients omit the param.
- **Haiku's one miss is the worst kind:** the vegan hard-constraint case
  (constraint dropped from tool args) — precisely ADR-100's adherence
  gate, and consistent with the project's "prompt rules guarding real
  cost need code backstops" history. One case is weak evidence; expand
  the hard-constraint cases from real traces before acting.
- Luna's perfect routing at $2/1k is intriguing but unproven where it
  matters for this role (multi-step tool loops, answer voice) — do not
  swap the orchestrator on a routing test alone.
- Sonnet 5's p50 is ~5x Haiku's — fine for the advanced tier, a real
  cost for the default tier's every-turn latency.

**This also unblocks the agent.txt trim**: `bakeoff --role orchestrator
--prompt <trimmed>` is now the parity gate ADR-174 required.

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
