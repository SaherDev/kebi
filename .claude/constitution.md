# Constitution Check Process

**Purpose:** Verify plans align with project's architectural decisions (ADRs) before implementation begins.

**When:** Step 2 (Plan) — Before proceeding to Implement

**Cost:** ~1K tokens (part of Plan step)

---

## What is Constitution?

Your project's **non-negotiable architectural constraints** documented as accepted ADRs in `docs/decisions.md`.

Constitution prevents:
- Architectural drift (using wrong tech stack)
- Boundary violations (wrong repo owns logic)
- Rework (catching issues in Plan phase, not Implement phase)

---

## Constitution Check Process

**Step 1: Identify Relevant ADRs**

Read your plan. Which ADRs does it relate to? Examples:

```
Plan: Add real-time notifications

Related ADRs:
- ADR-002: Separate AI repo (does this touch kebi? No ✓)
- ADR-004: Clerk (does this affect auth? No ✓)
- ADR-011: PORT env-var (does this change bootstrap? No ✓)
- ADR-014: One module per domain (should notifications be a module? Yes ✓)
```

**Step 2: Check Compliance**

For each ADR, ask: **Does my plan violate this decision?**

```
ADR-001: Nx over Turborepo
  Plan uses Nx? YES ✓ PASS

ADR-003: YAML config over dotenv
  Plan uses YAML for non-secrets? YES ✓ PASS

ADR-004: Clerk over custom auth
  Plan uses Clerk? YES ✓ PASS

ADR-014: One NestJS module per domain
  Plan creates NotificationsModule? YES ✓ PASS
```

**Step 3: Record in Plan**

Add Constitution Check section at top of plan doc:

```markdown
## Constitution Check

- [ ] ADR-001: Nx over Turborepo ✅ PASS
- [ ] ADR-003: YAML config ✅ PASS
- [ ] ADR-004: Clerk auth ✅ PASS
- [ ] ADR-014: Module organization ✅ PASS

**Result:** No violations. Proceed to Implement.
```

---

## If Violation Found: ❌ ERROR

**You cannot proceed if plan violates an accepted ADR.**

**Two options:**

### Option A: Revise Plan
Redesign plan to align with the constraint.

Example:
```
Plan violates ADR-003 (YAML config only):
  ❌ Plan: "Use environment variables for database URL"
  ✅ Revised: "Use config/dev.yml for database URL"
```

### Option B: Create New ADR to Supersede
If you must override the old decision, create a new ADR explaining why.

Example:
```markdown
## ADR-XXX: Switch from YAML to .env files

**Date:** 2026-03-10
**Status:** accepted (supersedes ADR-003)
**Context:** ADR-003 said YAML only. New requirement for dynamic env var injection breaks YAML approach.
**Decision:** Allow .env files for this specific use case (CI/CD secrets).
**Consequences:** Config management now split between YAML and .env.
```

Then update plan:
```markdown
## Constitution Check

- [ ] ADR-003: YAML config ⚠️ SUPERSEDED by ADR-XXX
- [ ] ADR-XXX: .env files for CI/CD secrets ✅ PASS
```

---

## Common ADRs to Check

### Architecture & Boundaries
- **ADR-002:** Separate AI repo (kebi is independent)
- **ADR-014:** One NestJS module per domain
- **ADR-001:** Nx monorepo tool

### Tech Stack (Locked)
- **ADR-003:** YAML config for non-secrets (not .env)
- **ADR-004:** Clerk for auth (not custom JWT)
- **ADR-030:** Alembic owns AI tables; product repo owns users/user_settings via TypeORM
- **ADR-007:** Tailwind v3 + shadcn/ui (not other libraries)
- **ADR-020:** pnpm package manager (not yarn/npm)

### Patterns & Practices
- **ADR-012:** YAML ConfigModule for non-secret config
- **ADR-013:** Global Clerk auth guard with @Public() opt-out
- **ADR-021:** Bruno over Swagger for API docs
- **ADR-023:** @Serialize() decorator for responses
- **ADR-024:** Zustand for client UI state

### Security & Data
- **ADR-022:** AiEnabledGuard with per-user flag + global kill switch
- **ADR-019:** Forward-compatible DTOs for AI responses

---

## Template for Plan Doc

```markdown
# [Feature] Implementation Plan

**Goal:** [one sentence]
**Architecture:** [2-3 sentences]

---

## Constitution Check

**Relevant ADRs:**
- [ ] ADR-XXX: [constraint name] ✅ COMPLIES
- [ ] ADR-XXX: [constraint name] ✅ COMPLIES
- [ ] ADR-XXX: [constraint name] ✅ COMPLIES

**Violations:** None

**Proceed:** ✅ YES

---

[Rest of plan...]
```

---

## When Constitution Check Delays You

**Q: What if constitution check reveals the constraint is wrong?**

A: Create a new ADR to supersede it. Document why the old decision no longer applies. This is part of architecture evolution — expected and healthy.

---

## Key Rules

- ✅ Every plan must check Constitution (Step 2, Plan phase)
- ✅ Check happens BEFORE implementation (Step 3)
- ❌ Never implement if Constitution Check fails
- ✅ Violations require either plan revision OR new ADR
- ✅ Recording Constitution Check in plan is mandatory
