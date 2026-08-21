"""Measure prompt sizes and enforce eval-gated growth budgets (ADR-174).

Every prompt in `config/prompts/` gets a token budget in
`config/evals/prompt_budgets.yaml`. Growth past a budget is a deliberate
act (raise the budget in the same change, with the eval evidence that
justifies it) — never a drive-by paragraph:

    poetry run python -m kebi.eval.prompt_audit
    poetry run python -m kebi.eval.prompt_audit --fail-on-budget   # CI gate

Estimated tokens are chars/4 — crude but stable, and budgets are set
against the same estimator so the comparison is apples-to-apples.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel

from kebi.core.config import find_project_root

_CHARS_PER_TOKEN = 4
_DEFAULT_BUDGETS = "config/evals/prompt_budgets.yaml"


class PromptMetric(BaseModel):
    name: str
    file: str
    characters: int
    estimated_tokens: int
    nonempty_lines: int
    # Non-empty lines appearing more than once in the same prompt —
    # the cheapest duplication smell (ADR-135: cuts are restatements).
    duplicate_lines: int
    budget_tokens: int | None = None
    within_budget: bool = True


class PromptAuditReport(BaseModel):
    total_estimated_tokens: int
    prompts: list[PromptMetric]
    over_budget: list[str]


def _load_budgets(budget_path: Path) -> dict[str, int]:
    if not budget_path.exists():
        return {}
    raw = yaml.safe_load(budget_path.read_text()) or {}
    limits = raw.get("limits") or {}
    return {
        name: int(entry["max_estimated_tokens"])
        for name, entry in limits.items()
        if isinstance(entry, dict) and "max_estimated_tokens" in entry
    }


def audit_prompts(budget_path: Path | None = None) -> PromptAuditReport:
    """Measure every prompt file against its budget."""
    root = find_project_root()
    budgets = _load_budgets(budget_path or root / _DEFAULT_BUDGETS)
    prompts_dir = root / "config" / "prompts"
    metrics: list[PromptMetric] = []
    for path in sorted(prompts_dir.glob("*.txt")):
        text = path.read_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        dupes = sum(count - 1 for count in Counter(lines).values() if count > 1)
        estimated = len(text) // _CHARS_PER_TOKEN
        budget = budgets.get(path.stem)
        metrics.append(
            PromptMetric(
                name=path.stem,
                file=str(path.relative_to(root)),
                characters=len(text),
                estimated_tokens=estimated,
                nonempty_lines=len(lines),
                duplicate_lines=dupes,
                budget_tokens=budget,
                within_budget=budget is None or estimated <= budget,
            )
        )
    return PromptAuditReport(
        total_estimated_tokens=sum(m.estimated_tokens for m in metrics),
        prompts=metrics,
        over_budget=[m.name for m in metrics if not m.within_budget],
    )


def _render(report: PromptAuditReport) -> str:
    lines = [
        "| prompt | est. tokens | budget | lines | dup lines | ok |",
        "|---|---|---|---|---|---|",
    ]
    for m in sorted(report.prompts, key=lambda m: m.estimated_tokens, reverse=True):
        lines.append(
            f"| {m.name} | {m.estimated_tokens} "
            f"| {m.budget_tokens if m.budget_tokens is not None else '—'} "
            f"| {m.nonempty_lines} | {m.duplicate_lines} "
            f"| {'✓' if m.within_budget else 'OVER'} |"
        )
    lines.append(f"\nTotal estimated tokens: {report.total_estimated_tokens}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budgets",
        default=None,
        help=f"budgets YAML (default: {_DEFAULT_BUDGETS})",
    )
    parser.add_argument(
        "--fail-on-budget",
        action="store_true",
        help="exit non-zero when any prompt exceeds its budget",
    )
    args = parser.parse_args()
    report = audit_prompts(Path(args.budgets) if args.budgets else None)
    print(_render(report))
    if args.fail_on_budget and report.over_budget:
        print(
            f"Prompt token budgets exceeded: {', '.join(report.over_budget)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
