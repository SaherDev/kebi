# CLAUDE.md Structure Rule

The root CLAUDE.md must have exactly six sections, in this order. No extra sections. No missing sections.

1. **Common Commands** — Dev server, tests, lint, build, deploy.
2. **Project Context** — What this repo is, stack, purpose. One paragraph.
3. **Key Directories** — Folder names and what each contains.
4. **Standards** — Path aliases, naming patterns, type conventions, architectural boundaries. Reference `.claude/rules/` files for details.
5. **Workflow** — 3 questions before touching code, plan, implement, verify, completion report with deviation flags.
6. **Notes** — Gotchas, silent failures, intentional quirks.

Rules:
- Keep the file under 150 lines. Move detailed standards to `.claude/rules/` files and reference them here.
- One line per rule: the rule plus a short reason. Rules that exist verbatim in an `@import`ed `.claude/rules/` file are not repeated in CLAUDE.md.
- No linter-enforceable rules (naming case, formatting, type-checker strictness) — those belong in ruff/mypy config.
- Content that doesn't fit a section gets folded into the closest match (e.g., commit conventions → Standards, deployment → Notes, token efficiency → Workflow).
- Do not add new top-level sections. If new content is needed, append it to the appropriate existing section.
- No phase language. This project does not use phase gates or phase-based scoping. Work is task-by-task, feature-by-feature. Each task arrives scoped; execute it.
