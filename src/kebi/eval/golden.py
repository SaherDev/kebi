"""Golden-set fixtures for the model bakeoff (ADR-175).

A golden case is one real (or curated) input for a role plus the expected
output facts worth holding a model to. Fixtures live in
`config/evals/golden/<role>/*.yaml`:

    cases:
      - id: explicit-city
        note: bare city name in the message
        input: { current_message: "tacos in mexico city", ... }
        expected: { city: mexico city, scope_tier: city }

`scripts/export_golden_traces.py` drafts cases from real Langfuse traces;
a human curates before committing (expected values are assertions, not
whatever the current model happened to say).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from kebi.core.config import find_project_root

_GOLDEN_DIR = "config/evals/golden"


class GoldenCase(BaseModel):
    id: str
    input: dict[str, Any]
    expected: dict[str, Any]
    note: str = ""


class GoldenSuite(BaseModel):
    role: str
    cases: list[GoldenCase] = Field(default_factory=list)


def load_suite(role: str, golden_dir: Path | None = None) -> GoldenSuite:
    """Load and merge every fixture file for `role`, sorted by filename.

    Raises FileNotFoundError when the role has no fixture directory —
    a bakeoff without a golden set is not a measurement.
    """
    base = golden_dir or find_project_root() / _GOLDEN_DIR
    role_dir = base / role
    if not role_dir.is_dir():
        raise FileNotFoundError(
            f"No golden set at {role_dir} — draft one with "
            "scripts/export_golden_traces.py and curate it before benchmarking."
        )
    cases: list[GoldenCase] = []
    for path in sorted(role_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        for entry in raw.get("cases") or []:
            cases.append(GoldenCase(**entry))
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"Duplicate golden case id {case.id!r} in {role_dir}")
        seen.add(case.id)
    return GoldenSuite(role=role, cases=cases)
