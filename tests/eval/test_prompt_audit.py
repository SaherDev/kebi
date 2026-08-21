"""Prompt audit — sizes measured, budgets enforced (ADR-174)."""

from __future__ import annotations

from pathlib import Path

from kebi.eval.prompt_audit import audit_prompts


def test_audit_covers_every_prompt_file() -> None:
    report = audit_prompts()
    from kebi.core.config import find_project_root

    files = {p.stem for p in (find_project_root() / "config/prompts").glob("*.txt")}
    assert {m.name for m in report.prompts} == files
    assert report.total_estimated_tokens > 0


def test_committed_budgets_hold() -> None:
    """The committed budgets file must pass — prompt growth past a budget
    belongs in the same change that raises the budget, with eval evidence."""
    report = audit_prompts()
    assert report.over_budget == [], (
        f"prompts over budget: {report.over_budget} — either trim the prompt "
        "or raise its budget in config/evals/prompt_budgets.yaml with the "
        "eval evidence that justifies the growth"
    )
    # Every prompt has a budget — an unbudgeted prompt can grow unnoticed.
    unbudgeted = [m.name for m in report.prompts if m.budget_tokens is None]
    assert unbudgeted == [], f"prompts missing budgets: {unbudgeted}"


def test_missing_budgets_file_reports_without_enforcement(tmp_path: Path) -> None:
    report = audit_prompts(tmp_path / "nope.yaml")
    assert report.over_budget == []
    assert all(m.budget_tokens is None for m in report.prompts)
