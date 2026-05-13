"""Post-cutover parity test against pre-cutover baselines (spec 030 Phase 5).

Asserts three SC gates from the spec:

- SC-007 (save-count floor): per fixture, post.saved_count >= baseline
  saved_count + needs_review_count. Under ADR-071 the post-cutover
  flow saves every picker output, so the new save count is at least
  what the legacy save + needs_review bands would have stored.
- SC-005 (latency p95): post p95 across the fixture set <= baseline p95 +
  5% noise margin.
- SC-006 (response field-set parity): for each completed fixture, the
  envelope's key-set matches the baseline modulo the three documented
  changes — `place_type` → `categories`, `attributes` → `tags`,
  and the `needs_review` literal is no longer emitted.

Gated by `KEBI_V2_PARITY=1` because the test exercises the live
pipeline (Google Places, OpenAI, Voyage, Apify, Groq) and requires
.env + docker compose running. Default CI run skips.

Baselines are captured pre-cutover via
`scripts/v2_cutover_baseline_partition.py` and live in
`tests/core/extraction/fixtures/v2_cutover/baselines/`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from statistics import quantiles
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FIXTURE_DIR = REPO_ROOT / "tests/core/extraction/fixtures/v2_cutover"
INPUTS_FILE = FIXTURE_DIR / "inputs.json"
BASELINES_DIR = FIXTURE_DIR / "baselines"
ENVELOPES_DIR = BASELINES_DIR / "response_envelopes"

LATENCY_OVERHEAD_FACTOR = 1.05  # SC-005 — allow 5% noise margin.
REQUEST_TIMEOUT_SECONDS = 180.0  # extraction can be slow on cold cache.

# Three intentional externally observable changes (contracts/http-response-parity.md).
LEGACY_PLACE_KEYS_REMOVED = {"place_id", "place_type", "attributes", "enriched"}
V2_PLACE_KEYS_ADDED = {"id", "categories", "tags", "place_name_aliases", "refreshed_at"}

_PARITY_FLAG = os.environ.get("KEBI_V2_PARITY", "")
_LIVE = _PARITY_FLAG in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live parity test gated by KEBI_V2_PARITY=1. Requires docker compose "
        "running + .env populated. Run baselines via "
        "scripts/v2_cutover_baseline_partition.py before this."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baselines() -> dict[str, Any]:
    """Load all pre-cutover baselines from disk."""
    partition_counts_file = BASELINES_DIR / "partition_counts.json"
    latency_file = BASELINES_DIR / "latency_ms.json"
    if not partition_counts_file.exists() or not latency_file.exists():
        pytest.skip(
            "Baselines not captured. Run "
            "`poetry run python scripts/v2_cutover_baseline_partition.py` "
            "first."
        )
    return {
        "partition_counts": json.loads(partition_counts_file.read_text()),
        "latency_ms": json.loads(latency_file.read_text()),
        "envelopes": {
            f.stem: json.loads(f.read_text())
            for f in ENVELOPES_DIR.glob("*.json")
        },
    }


@pytest.fixture(scope="module")
def fixture_inputs() -> dict[str, Any]:
    data = json.loads(INPUTS_FILE.read_text())
    return data


@pytest.fixture(scope="module")
async def post_cutover_envelopes(
    fixture_inputs: dict[str, Any],
) -> dict[str, tuple[dict[str, Any], float]]:
    """Run every fixture through the in-process app once. Cached for the
    test module so the three assertions share one set of API calls.
    """
    from kebi.api.main import app

    user_id = fixture_inputs.get("user_id", "v2-cutover-parity-user")
    transport = httpx.ASGITransport(app=app)
    out: dict[str, tuple[dict[str, Any], float]] = {}

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        for fixture in fixture_inputs["fixtures"]:
            fid = fixture["id"]
            start = time.perf_counter()
            try:
                r = await client.post(
                    "/v1/extract",
                    json={"user_id": user_id, "raw_input": fixture["raw_input"]},
                )
                envelope = (
                    r.json()
                    if r.status_code == 200
                    else {
                        "status": "failed",
                        "results": [],
                        "raw_input": fixture["raw_input"],
                        "failure_reason": "pipeline_error",
                        "failure_message": f"HTTP {r.status_code}",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                envelope = {
                    "status": "failed",
                    "results": [],
                    "raw_input": fixture["raw_input"],
                    "failure_reason": "pipeline_error",
                    "failure_message": f"{type(exc).__name__}: {exc}",
                }
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            out[fid] = (envelope, elapsed_ms)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _count_saved(envelope: dict[str, Any]) -> int:
    """Count saved-band items (saved + needs_review in legacy,
    saved only post-cutover)."""
    if envelope.get("status") != "completed":
        return 0
    return sum(
        1
        for r in envelope.get("results", [])
        if r.get("status") in ("saved", "needs_review")
    )


@pytest.mark.asyncio
async def test_save_count_floor_per_fixture(
    baselines: dict[str, Any],
    post_cutover_envelopes: dict[str, tuple[dict[str, Any], float]],
) -> None:
    """SC-007: post.saved >= baseline.saved + baseline.needs_review per fixture.

    Under ADR-071 every picker output is saved, so the post-cutover
    save count is the union of the legacy save and needs_review bands
    (modulo picker-vocabulary differences). A drop in count is a
    regression.
    """
    regressions: list[str] = []
    for fid, baseline_counts in baselines["partition_counts"].items():
        baseline_floor = baseline_counts.get("saved", 0) + baseline_counts.get(
            "needs_review", 0
        )
        envelope, _ = post_cutover_envelopes.get(fid, ({}, 0.0))
        post_count = _count_saved(envelope)
        if post_count < baseline_floor:
            regressions.append(
                f"{fid}: post={post_count} < baseline floor={baseline_floor} "
                f"(baseline saved={baseline_counts.get('saved', 0)}, "
                f"needs_review={baseline_counts.get('needs_review', 0)})"
            )
    assert not regressions, (
        "Save-count regression on these fixtures:\n  "
        + "\n  ".join(regressions)
    )


@pytest.mark.asyncio
async def test_latency_p95_within_margin(
    baselines: dict[str, Any],
    post_cutover_envelopes: dict[str, tuple[dict[str, Any], float]],
) -> None:
    """SC-005: post p95 <= baseline p95 * 1.05."""
    baseline_p95 = float(baselines["latency_ms"]["p95_ms"])
    if baseline_p95 == 0.0:
        pytest.skip("Baseline p95 is 0 (no fixtures completed pre-cutover).")

    latencies = [
        elapsed for _, elapsed in post_cutover_envelopes.values() if elapsed > 0
    ]
    if not latencies:
        pytest.fail("Post-cutover produced zero non-zero latencies.")

    post_p95 = (
        quantiles(latencies, n=100)[94]
        if len(latencies) >= 2
        else latencies[0]
    )
    threshold = baseline_p95 * LATENCY_OVERHEAD_FACTOR
    assert post_p95 <= threshold, (
        f"p95 regression: post={post_p95:.1f}ms > "
        f"baseline+5% threshold={threshold:.1f}ms "
        f"(baseline={baseline_p95:.1f}ms)"
    )


@pytest.mark.asyncio
async def test_envelope_field_set_parity_modulo_documented_changes(
    baselines: dict[str, Any],
    post_cutover_envelopes: dict[str, tuple[dict[str, Any], float]],
) -> None:
    """SC-006: per fixture, envelope key-set matches baseline modulo the
    three documented changes (place_type→categories, attributes→tags,
    no needs_review emission)."""
    divergences: list[str] = []

    for fid, baseline_envelope in baselines["envelopes"].items():
        post_envelope, _ = post_cutover_envelopes.get(fid, ({}, 0.0))
        baseline_keys = set(baseline_envelope.keys())
        post_keys = set(post_envelope.keys())

        if baseline_keys != post_keys:
            divergences.append(
                f"{fid}: envelope keys differ. "
                f"missing={baseline_keys - post_keys}, "
                f"extra={post_keys - baseline_keys}"
            )
            continue

        if (
            baseline_envelope.get("status") != "completed"
            or post_envelope.get("status") != "completed"
        ):
            continue  # both failed → envelope-level parity already checked.

        baseline_items = baseline_envelope.get("results", [])
        post_items = post_envelope.get("results", [])
        if not baseline_items or not post_items:
            continue

        for idx, (b_item, p_item) in enumerate(
            zip(baseline_items, post_items, strict=False)
        ):
            b_keys = set(b_item.keys())
            p_keys = set(p_item.keys())
            if b_keys != p_keys:
                divergences.append(
                    f"{fid}[{idx}]: item keys differ. "
                    f"missing={b_keys - p_keys}, extra={p_keys - b_keys}"
                )

            b_place = b_item.get("place") or {}
            p_place = p_item.get("place") or {}
            b_place_keys = set(b_place.keys()) - LEGACY_PLACE_KEYS_REMOVED
            p_place_keys = set(p_place.keys()) - V2_PLACE_KEYS_ADDED
            if b_place_keys - p_place_keys:
                divergences.append(
                    f"{fid}[{idx}].place: baseline has keys post is missing: "
                    f"{b_place_keys - p_place_keys}"
                )
            if p_place_keys - b_place_keys:
                divergences.append(
                    f"{fid}[{idx}].place: post has keys baseline didn't: "
                    f"{p_place_keys - b_place_keys}"
                )

            # status literal — no needs_review post-cutover.
            if p_item.get("status") == "needs_review":
                divergences.append(
                    f"{fid}[{idx}]: post emitted status='needs_review' "
                    f"(ADR-071 supersedes this; only saved/duplicate allowed)"
                )

    assert not divergences, (
        "Externally observed envelope shape diverged beyond the three "
        "documented changes:\n  " + "\n  ".join(divergences)
    )
