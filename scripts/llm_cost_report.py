"""Per-role LLM cost/latency/retry report from the Langfuse API (ADR-172).

The before/after instrument for every model swap and prompt change:

    poetry run python scripts/llm_cost_report.py --days 7
    poetry run python scripts/llm_cost_report.py --from 2026-08-01 --to 2026-08-20

Pulls GENERATION observations from Langfuse's public API (paginated),
groups them by observation name (one name per role call site — e.g.
`agent.orchestrator`, `extraction.llm_picker`, `taste_regen.llm`), and
prints a markdown table: calls, error spans (= retry attempts +
exhaustions), input/output/cached tokens, USD cost, p50/p95 latency.

Auth comes from the same env vars the app uses (LANGFUSE_PUBLIC_KEY,
LANGFUSE_SECRET_KEY, LANGFUSE_HOST) — run it wherever `.env` resolves.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from kebi.core.config import get_env


class _RoleAgg(BaseModel):
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    latencies_ms: list[float] = []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="lookback window")
    parser.add_argument("--from", dest="from_date", help="ISO date, overrides --days")
    parser.add_argument("--to", dest="to_date", help="ISO date (default: now)")
    return parser.parse_args()


def _window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    to_time = (
        datetime.fromisoformat(args.to_date).replace(tzinfo=UTC)
        if args.to_date
        else datetime.now(UTC)
    )
    from_time = (
        datetime.fromisoformat(args.from_date).replace(tzinfo=UTC)
        if args.from_date
        else to_time - timedelta(days=args.days)
    )
    return from_time, to_time


async def _fetch_observations(
    client: httpx.AsyncClient, from_time: datetime, to_time: datetime
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    page = 1
    while True:
        response = await client.get(
            "/api/public/observations",
            params={
                "type": "GENERATION",
                "fromStartTime": from_time.isoformat(),
                "toStartTime": to_time.isoformat(),
                "page": page,
                "limit": 100,
            },
        )
        response.raise_for_status()
        payload = response.json()
        observations.extend(payload.get("data", []))
        meta = payload.get("meta", {})
        if page >= int(meta.get("totalPages", 1) or 1):
            return observations
        page += 1


def _latency_ms(obs: dict[str, Any]) -> float | None:
    start, end = obs.get("startTime"), obs.get("endTime")
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end.replace("Z", "+00:00")) - (
            datetime.fromisoformat(start.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    return delta.total_seconds() * 1000


def _aggregate(observations: list[dict[str, Any]]) -> dict[str, _RoleAgg]:
    by_name: dict[str, _RoleAgg] = {}
    for obs in observations:
        agg = by_name.setdefault(obs.get("name") or "(unnamed)", _RoleAgg())
        agg.calls += 1
        if obs.get("level") == "ERROR":
            agg.errors += 1
        usage = obs.get("usageDetails") or obs.get("usage") or {}
        agg.input_tokens += int(usage.get("input", 0) or 0)
        agg.output_tokens += int(usage.get("output", 0) or 0)
        agg.cache_read_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
        agg.cache_write_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        cost = obs.get("calculatedTotalCost")
        if cost is not None:
            agg.cost_usd += float(cost)
        elif usage:
            agg.unpriced_calls += 1
        latency = _latency_ms(obs)
        if latency is not None:
            agg.latencies_ms.append(latency)
    return by_name


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100)[max(int(fraction * 100) - 1, 0)]


def _render(
    by_name: dict[str, _RoleAgg], from_time: datetime, to_time: datetime
) -> str:
    lines = [
        f"# LLM cost report — {from_time.date()} → {to_time.date()}",
        "",
        "| call site | calls | errors | input | output | cache read | cache write "
        "| cost USD | unpriced | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    total_cost = 0.0
    for name, agg in sorted(
        by_name.items(), key=lambda kv: kv[1].cost_usd, reverse=True
    ):
        total_cost += agg.cost_usd
        lines.append(
            f"| {name} | {agg.calls} | {agg.errors} | {agg.input_tokens} "
            f"| {agg.output_tokens} | {agg.cache_read_tokens} "
            f"| {agg.cache_write_tokens} | {agg.cost_usd:.4f} "
            f"| {agg.unpriced_calls} "
            f"| {_percentile(agg.latencies_ms, 0.50):.0f} "
            f"| {_percentile(agg.latencies_ms, 0.95):.0f} |"
        )
    lines += ["", f"**Total cost: ${total_cost:.4f}**"]
    return "\n".join(lines)


async def _main() -> int:
    args = _parse_args()
    from_time, to_time = _window(args)
    env = get_env()
    if not env.LANGFUSE_PUBLIC_KEY or not env.LANGFUSE_SECRET_KEY:
        print("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set", file=sys.stderr)
        return 1
    async with httpx.AsyncClient(
        base_url=env.LANGFUSE_HOST or "https://cloud.langfuse.com",
        auth=(env.LANGFUSE_PUBLIC_KEY, env.LANGFUSE_SECRET_KEY),
        timeout=30.0,
    ) as client:
        observations = await _fetch_observations(client, from_time, to_time)
    print(_render(_aggregate(observations), from_time, to_time))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
