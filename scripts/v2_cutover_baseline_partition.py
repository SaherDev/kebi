"""Capture pre-cutover baselines for spec 030 (places_v2 migration).

Reads fixtures from `tests/core/extraction/fixtures/v2_cutover/inputs.json`,
POSTs each one through the in-process FastAPI app at `POST /v1/extract`,
and writes three baseline artifacts that the post-cutover parity test
(`tests/core/extraction/test_v2_cutover_parity.py`) compares against:

- partition_counts.json — per-fixture (saved, needs_review, duplicate, dropped) counts
- latency_ms.json — per-fixture wall time + p95 across the set
- response_envelopes/<id>.json — per-fixture serialized ExtractPlaceResponse

MUST be run on `dev` HEAD or the Phase 2 commit (pre-Phase 3) — both
produce identical legacy behavior because Phase 2's `_to_legacy_source`
shim in service.py keeps the persistence layer behavior-neutral.

Prereqs:
- `docker compose up -d` (Postgres + Redis running)
- `.env` populated with API keys (OpenAI, Google, Voyage, Apify, Groq, Anthropic)
- Real working URLs in `tests/core/extraction/fixtures/v2_cutover/inputs.json`
  (the committed file has stubs; replace with URLs you'd expect to extract).

Usage:
    poetry run python scripts/v2_cutover_baseline_partition.py
    # add --strict to fail the whole run when any fixture errors at the HTTP
    # layer (transport / 500). Default mode keeps going and records the
    # error envelope as the baseline for that fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from statistics import quantiles

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("v2_cutover_baseline")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests/core/extraction/fixtures/v2_cutover"
INPUTS_FILE = FIXTURE_DIR / "inputs.json"
BASELINES_DIR = FIXTURE_DIR / "baselines"
ENVELOPES_DIR = BASELINES_DIR / "response_envelopes"

REQUEST_TIMEOUT_SECONDS = 180.0  # extraction can be slow on cold cache + vision


def _partition_counts(envelope: dict) -> dict[str, int]:
    """Count per-status items in a serialized envelope.

    Envelope-level `failed` is recorded as a single dropped count so the
    user-visible "request produced nothing" outcome aggregates cleanly.
    """
    counts = {"saved": 0, "needs_review": 0, "duplicate": 0, "dropped": 0}
    if envelope.get("status") == "failed":
        counts["dropped"] = 1
        return counts
    for item in envelope.get("results", []):
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return counts


async def _run_one(
    client: httpx.AsyncClient, raw_input: str, user_id: str
) -> tuple[dict, float]:
    """POST a single fixture through /v1/extract via the in-process ASGI app.

    Returns (response_json, wall_ms). Raises only on transport-level
    failures (bad ASGI wiring, app import error) — application-level
    failures arrive as `status="failed"` envelopes from the route itself.
    """
    start = time.perf_counter()
    r = await client.post(
        "/v1/extract",
        json={"user_id": user_id, "raw_input": raw_input},
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    if r.status_code != 200:
        # Route raised a non-200; the request payload may be malformed.
        # Record it explicitly so the baseline records the validation failure.
        return {
            "status": "failed",
            "results": [],
            "raw_input": raw_input,
            "failure_reason": "pipeline_error",
            "failure_message": f"HTTP {r.status_code}: {r.text[:300]}",
        }, elapsed_ms
    return r.json(), elapsed_ms


async def main(strict: bool) -> int:
    if not INPUTS_FILE.exists():
        logger.error("Fixture file not found: %s", INPUTS_FILE)
        return 2

    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    ENVELOPES_DIR.mkdir(parents=True, exist_ok=True)

    data = json.loads(INPUTS_FILE.read_text())
    user_id = data.get("user_id", "v2-cutover-fixture-user")
    fixtures = data.get("fixtures", [])

    # Import inside main so a broken import surfaces with a real traceback
    # instead of leaving stale baselines untouched.
    from kebi.api.main import app

    transport = httpx.ASGITransport(app=app)
    partition_counts: dict[str, dict[str, int]] = {}
    latency_ms: dict[str, float] = {}
    transport_failures: list[str] = []

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        for fixture in fixtures:
            fid = fixture["id"]
            logger.info("Running fixture: %s", fid)
            try:
                envelope, elapsed = await _run_one(
                    client, raw_input=fixture["raw_input"], user_id=user_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "TRANSPORT FAILURE on %s: %s: %s",
                    fid,
                    type(exc).__name__,
                    exc,
                )
                transport_failures.append(fid)
                if strict:
                    return 3
                envelope = {
                    "status": "failed",
                    "results": [],
                    "raw_input": fixture["raw_input"],
                    "failure_reason": "pipeline_error",
                    "failure_message": (
                        f"transport: {type(exc).__name__}: {exc}"
                    ),
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
        json.dumps(
            {"per_fixture": latency_ms, "p95_ms": p95}, indent=2, sort_keys=True
        )
    )

    logger.info("Wrote baselines to %s", BASELINES_DIR)
    logger.info("p95 latency across set: %.2fms", p95)
    if transport_failures:
        logger.warning(
            "%d fixture(s) failed at the transport layer (not the pipeline): %s",
            len(transport_failures),
            ", ".join(transport_failures),
        )
        logger.warning(
            "Re-run with `--strict` if the cause needs investigation before merging."
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail the whole run on any transport-layer error (default: keep going).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(strict=args.strict)))
