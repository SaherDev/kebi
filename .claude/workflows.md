# Workflows — Kebi

## 5-Step Token-Efficient Workflow

Use this workflow for all tasks. Each step uses a specific Claude model to minimize token burn.

---

## Step 1: CLARIFY

**Model:** Haiku
**When:** Task is ambiguous (3+ unknowns)
**Cost:** ~2K tokens

**Template:**

```
Q1: [question]
Options: A) [option], B) [option], C) [option]
Recommended: A - [brief reason]

Q2: [question]
Options: A) [option], B) [option]
Recommended: B - [brief reason]

[Up to 5 questions max]
```

**Output:** Answers recorded in chat (no file created)

**If task is fully scoped:** Skip to Step 2

---

## Step 2: PLAN

**Model:** Sonnet
**When:** 3+ files OR crosses repo boundary
**Cost:** ~8K tokens

### Branch Setup (before planning)

1. Confirm you are on `dev`: `git branch --show-current`
2. Pull latest: `git pull origin dev`
3. Generate branch name from task type:
   - Spec-kit task: `<number>-<feature-name>` (e.g., `001-nx-monorepo-setup`)
   - Manual feature: `feature/<short-description>` (e.g., `feature/clerk-auth`)
   - Bug fix: `fix/<short-description>` (e.g., `fix/alembic-migration-order`)
4. Checkout: `git checkout -b <branch-name>`

> Scope examples: see `.claude/rules/git.md`

**Template:**

```markdown
# [Feature Name] Implementation Plan

**Goal:** [one sentence what this builds]

**Architecture:** [2-3 sentences on approach]

**Tech Stack:** [key technologies]

---

## Constitution Check

- [ ] ADR-XXX: [constraint name] ✅ Aligns
- [ ] ADR-XXX: [constraint name] ✅ Aligns
- [ ] No violations found

If any violation: ❌ ERROR — Do not proceed (create new ADR to supersede)

---

## Phase 1: [Name]

**Checklist:**

- [ ] Task 1.1: [description]
- [ ] Task 1.2: [description]

**Files:** path/to/file.ts, path/to/file.ts

**Verify:** command to run

---

## Phase 2: [Name]

**Checklist:**

- [ ] Task 2.1: [description]
- [ ] Task 2.2: [description]

**Files:** path/to/file.ts

**Verify:** command to run

---

## Verify All

- [ ] Run: `pnpm nx affected -t test,lint` (or per-repo verify)
- [ ] All tests pass
- [ ] All lint passes
- [ ] All types check
```

**Output:** File saved to `docs/plans/YYYY-MM-DD-<feature>.md`

**If simple (1-2 files):** Skip to Step 3

---

## Step 3: IMPLEMENT

**Model:** Haiku (default) or Sonnet (if complex logic)
**When:** Always
**Cost:** ~2K tokens (Haiku) or ~8K tokens (Sonnet)

**Process:**

1. Follow the plan checklist
2. For each phase:
   - [ ] Write test (if applicable)
   - [ ] Implement code/config
   - [ ] Run verify command from plan
   - [ ] Commit: `type(scope): description` (see `.claude/rules/git.md`)
3. No separate files created (just code + commits)

**Model Selection:**

- **Haiku:** Templates, configs, straightforward changes, deletions
- **Sonnet:** Complex algorithms, multi-step logic, architecture changes

---

## Step 4: VERIFY

**Model:** Haiku
**When:** Always (after Implement)
**Cost:** ~500 tokens

**Checklist:**

- [ ] Run verify commands from plan (e.g., `pnpm nx affected -t test,lint`)
- [ ] All tests pass
- [ ] All lint passes
- [ ] All types check
- [ ] All commits pushed

**If FAIL:** Go back to Step 3, fix issues, re-verify

**If PASS:** Continue to Step 5

---

## Step 5: COMPLETE

**Model:** Haiku
**When:** Always (after Verify passes)
**Cost:** ~100 tokens

**Process:**

1. Checklist: All items marked [X]
2. Push branch: `git push origin <branch-name>`
3. Optional: Add 2-3 line comment if complex

**No separate file created** — just mark task done

---

## Token Cost Summary

| Task Complexity                 | Clarify | Plan | Implement   | Verify | Complete | **Total**  |
| ------------------------------- | ------- | ---- | ----------- | ------ | -------- | ---------- |
| **Simple** (1-2 files)          | 2K      | SKIP | 2K (Haiku)  | 0.5K   | 0.1K     | **~4.5K**  |
| **Normal** (3+ files)           | 2K      | 8K   | 2K (Haiku)  | 0.5K   | 0.1K     | **~12.5K** |
| **Complex** (multi-repo, logic) | 2K      | 8K   | 8K (Sonnet) | 0.5K   | 0.1K     | **~18.5K** |

**Old approach (multi-subagent):** ~250K tokens per task
**New approach:** ~13K average tokens per task
**Savings:** ~95% token reduction

---

## Constitution Check (Step 2 Detail)

See `.claude/constitution.md` for full process.

**Quick version:**

1. Open `docs/decisions.md`
2. List ADRs related to your feature
3. For each ADR, verify: Does plan comply?
4. If any violation → ❌ ERROR, don't proceed
5. To override: Create new ADR explaining supersession

Common ADRs to check:

- ADR-001: Nx (monorepo tool locked)
- ADR-003: YAML config (format locked)
- ADR-004: Clerk (auth locked)
- ADR-030: Alembic owns AI tables; TypeORM (product repo) owns product tables
- ADR-012: ConfigModule (non-secret config)
- ADR-013: Auth guard (global boundary)
- ADR-014: Module organization (per-domain)

---

## Examples

### Example: Simple Task (Skip Plan)

**Task:** Fix typo in README

```
Step 1: Clarify → SKIP (fully scoped)
Step 2: Plan → SKIP (only 1 file)
Step 3: Implement (Haiku) → Edit README, commit
Step 4: Verify (Haiku) → git status ✓
Step 5: Complete (Haiku) → Mark done
Cost: ~2.5K tokens
```

### Example: Normal Task (Full Flow)

**Task:** Add user authentication to NestJS

```
Step 1: Clarify (Haiku) → Ask: JWT or Clerk? Answer: Clerk
Step 2: Plan (Sonnet) → Create plan with phases + Constitution Check
        └── Constitution: ADR-004 (Clerk locked) ✓, ADR-013 (guard) ✓
Step 3: Implement (Haiku) → Follow plan 4 phases, write code
Step 4: Verify (Haiku) → Run tests, lint, types ✓
Step 5: Complete (Haiku) → Mark done
Cost: ~13K tokens
```

---

## Spec-kit Alternative Workflow

For larger or less defined features, spec-kit can replace Steps 1–2:

| Spec-kit Command | Replaces | When |
|------------------|----------|------|
| `speckit.clarify` | Step 1 (Clarify) | Feature has unclear requirements |
| `speckit.specify` | Step 2 start | Write structured feature spec |
| `speckit.plan` | Step 2 middle | Generate implementation plan from spec |
| `speckit.tasks` | Step 2 end | Break plan into ordered tasks.md |
| `speckit.analyze` | Step 2 check | Verify spec/plan/tasks are consistent |
| `speckit.constitution` | Constitution Check | Validate plan against ADRs |
| `speckit.implement` | Step 3 (Implement) | Execute tasks from tasks.md |
| `speckit.taskstoissues` | Step 5 (Complete) | Convert tasks to GitHub issues |

**When to use spec-kit:** Large features with multiple unknowns, cross-cutting changes, or when a structured spec is needed before implementation.

**When to use the simple workflow:** Small, well-scoped tasks (1–3 files, clear requirements). The simple workflow is faster and sufficient for most day-to-day work.

---

## Key Principles

✅ **Haiku by default** — Use Sonnet only when logic is complex
✅ **Constitution Check** — Catches architectural violations early
✅ **Plan creates checklist** — Implementer just checks boxes
✅ **One source of truth** — Plan doc has everything needed
✅ **Direct execution** — No unnecessary agents or reviews
✅ **No per-task meta-files** — Just code, commits, task status
