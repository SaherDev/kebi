"""Draft golden-set cases from real Langfuse traces (ADR-175).

    poetry run python scripts/export_golden_traces.py --role location_resolver --days 14
    poetry run python scripts/export_golden_traces.py --role extractor --days 30

Pulls the role's GENERATION observations from the Langfuse API and writes
a DRAFT fixture to `config/evals/golden/<role>/drafts.yaml`. Drafts are
raw material, not a golden set: a human curates each case — fills the
`expected` block with assertions (not whatever the model happened to
say), trims PII, drops duplicates — then moves kept cases into a
committed fixture file and deletes the draft.

Spans traced with input scrubbing on (LANGFUSE_SCRUB_INPUT) or without an
`input` payload export as empty-input stubs — count them, don't curate
them; they signal which call sites need input attached to their spans.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import yaml

from kebi.core.config import find_project_root, get_env

_ROLE_SPAN_NAMES = {
    "location_resolver": "agent.location_resolver",
    "extractor": "extraction.llm_resolver",
    "orchestrator": "agent.orchestrator",
}


async def _fetch(role_span: str, days: int, limit: int) -> list[dict[str, Any]]:
    env = get_env()
    if not env.LANGFUSE_PUBLIC_KEY or not env.LANGFUSE_SECRET_KEY:
        raise SystemExit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=env.LANGFUSE_HOST or "https://cloud.langfuse.com",
        auth=(env.LANGFUSE_PUBLIC_KEY, env.LANGFUSE_SECRET_KEY),
        timeout=30.0,
    ) as client:
        page = 1
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        while len(rows) < limit:
            response = await client.get(
                "/api/public/observations",
                params={
                    "type": "GENERATION",
                    "name": role_span,
                    "fromStartTime": since,
                    "page": page,
                    "limit": min(100, limit),
                },
            )
            response.raise_for_status()
            payload = response.json()
            rows.extend(payload.get("data", []))
            if page >= int(payload.get("meta", {}).get("totalPages", 1) or 1):
                break
            page += 1
    return rows[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(_ROLE_SPAN_NAMES))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    rows = asyncio.run(_fetch(_ROLE_SPAN_NAMES[args.role], args.days, args.limit))
    cases, empty = [], 0
    for i, row in enumerate(rows):
        obs_input = row.get("input")
        if not obs_input:
            empty += 1
            continue
        cases.append(
            {
                "id": f"draft-{i:03d}",
                "note": f"DRAFT from trace {row.get('traceId', '')[:12]} — "
                "curate expected before committing",
                "input": obs_input,
                "expected": {},  # human fills with assertions
                "_observed_output": row.get("output"),  # aid only — delete
            }
        )

    out_dir = find_project_root() / "config/evals/golden" / args.role
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "drafts.yaml"
    out.write_text(
        yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False)
    )
    print(f"{len(cases)} draft case(s) → {out}")
    if empty:
        print(
            f"{empty} span(s) had no input payload (scrubbed or not attached) "
            "— skipped",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
