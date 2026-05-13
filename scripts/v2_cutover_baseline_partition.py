"""Capture pre-cutover baselines for spec 030 (places_v2 migration).

Reads fixtures from `tests/core/extraction/fixtures/v2_cutover/inputs.json`,
runs the current `ExtractionService.run` against each, and writes three
baseline artifacts that the post-cutover parity test
(`tests/core/extraction/test_v2_cutover_parity.py`) compares against:

- partition_counts.json — per-fixture (saved, needs_review, dropped) counts
- latency_ms.json — per-fixture wall time + p95 across the set
- response_envelopes/<id>.json — per-fixture serialized ExtractPlaceResponse

MUST be run on `dev` HEAD (pre-cutover code) before merging the migration.

Usage:
    poetry run python scripts/v2_cutover_baseline_partition.py

Requires API keys and a running Postgres+Redis (docker compose up -d).
The script writes into tests/core/extraction/fixtures/v2_cutover/baselines/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from statistics import quantiles
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("v2_cutover_baseline")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests/core/extraction/fixtures/v2_cutover"
INPUTS_FILE = FIXTURE_DIR / "inputs.json"
BASELINES_DIR = FIXTURE_DIR / "baselines"
ENVELOPES_DIR = BASELINES_DIR / "response_envelopes"


async def _run_extraction_for_fixture(
    fixture: dict[str, Any], user_id: str
) -> tuple[dict[str, Any], float]:
    """Run a single fixture through the live ExtractionService and return
    (serialized_response, wall_time_ms).

    Imports happen here (not at module top) so a partial / broken
    dependency state doesn't block invocation entirely.
    """
    from kebi.api.deps import (
        get_extraction_service,
    )

    service = await _resolve_service(get_extraction_service)
    start = time.perf_counter()
    response = await service.run(
        raw_input=fixture["raw_input"],
        user_id=user_id,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return response.model_dump(mode="json"), elapsed_ms


async def _resolve_service(factory: Any) -> Any:
    """`get_extraction_service` is a FastAPI Depends factory — call its
    underlying resolution manually here. Adjust if the factory shape
    differs in your version of `api/deps.py`."""
    from fastapi import Depends  # noqa: F401

    if callable(factory):
        result = factory() if not asyncio.iscoroutinefunction(factory) else await factory()
        if asyncio.iscoroutine(result):
            result = await result
        return result
    raise TypeError(f"Unsupported factory: {factory!r}")


def _partition_counts(envelope: dict[str, Any]) -> dict[str, int]:
    """Count saved / needs_review / duplicate from a serialized envelope."""
    counts = {"saved": 0, "needs_review": 0, "duplicate": 0, "dropped": 0}
    if envelope.get("status") == "failed":
        counts["dropped"] = 1  # nothing saved — treat the request as 0 saves
        return counts
    for item in envelope.get("results", []):
        status = item.get("status", "unknown")
        if status in counts:
            counts[status] += 1
    return counts


async def main() -> None:
    if not INPUTS_FILE.exists():
        sys.exit(
            f"Fixture file not found: {INPUTS_FILE}\n"
            "Populate it with real URLs / text before running."
        )

    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    ENVELOPES_DIR.mkdir(parents=True, exist_ok=True)

    data = json.loads(INPUTS_FILE.read_text())
    user_id = data.get("user_id", "v2-cutover-fixture-user")
    fixtures = data.get("fixtures", [])

    partition_counts: dict[str, dict[str, int]] = {}
    latency_ms: dict[str, float] = {}

    for fixture in fixtures:
        fid = fixture["id"]
        logger.info("Running fixture: %s", fid)
        try:
            envelope, elapsed = await _run_extraction_for_fixture(fixture, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fixture %s raised %s: %s", fid, type(exc).__name__, exc)
            envelope = {
                "status": "failed",
                "results": [],
                "raw_input": fixture["raw_input"],
                "failure_reason": "pipeline_error",
                "failure_message": f"{type(exc).__name__}: {exc}",
            }
            elapsed = 0.0

        (ENVELOPES_DIR / f"{fid}.json").write_text(
            json.dumps(envelope, indent=2, sort_keys=True)
        )
        partition_counts[fid] = _partition_counts(envelope)
        latency_ms[fid] = round(elapsed, 2)

    all_latencies = [v for v in latency_ms.values() if v > 0]
    p95 = (
        round(quantiles(all_latencies, n=100)[94], 2)
        if len(all_latencies) >= 2
        else (all_latencies[0] if all_latencies else 0.0)
    )

    (BASELINES_DIR / "partition_counts.json").write_text(
        json.dumps(partition_counts, indent=2, sort_keys=True)
    )
    (BASELINES_DIR / "latency_ms.json").write_text(
        json.dumps({"per_fixture": latency_ms, "p95_ms": p95}, indent=2, sort_keys=True)
    )

    logger.info("Wrote baselines to %s", BASELINES_DIR)
    logger.info("p95 latency across set: %.2fms", p95)


if __name__ == "__main__":
    asyncio.run(main())
