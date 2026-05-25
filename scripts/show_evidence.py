"""Inspect the evidence ledger for one place.

Usage:
    poetry run python scripts/show_evidence.py <place_id>
    poetry run python scripts/show_evidence.py <place_id> --raw

`<place_id>` is the UUID stored on `places.id` (and what extraction returns
as `result.place.id`). `--raw` dumps each event verbatim instead of the
compact summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter

from kebi.core.config import get_env
from kebi.core.extraction.evidence_bucket import EvidenceBucketReader
from kebi.providers.object_storage import NullObjectStorage, S3ObjectStorage


def _build_storage() -> S3ObjectStorage | NullObjectStorage:
    env = get_env()
    if not (
        env.BUCKET_NAME
        and env.BUCKET_ACCESS_KEY_ID
        and env.BUCKET_SECRET_ACCESS_KEY
    ):
        print("warning: BUCKET_* env vars not set — using NullObjectStorage", file=sys.stderr)
        return NullObjectStorage()
    return S3ObjectStorage(
        bucket=env.BUCKET_NAME,
        endpoint_url=env.BUCKET_ENDPOINT_URL,
        access_key_id=env.BUCKET_ACCESS_KEY_ID,
        secret_access_key=env.BUCKET_SECRET_ACCESS_KEY,
        region=env.BUCKET_REGION,
    )


async def main(place_id: str, raw: bool) -> int:
    storage = _build_storage()
    reader = EvidenceBucketReader(storage)
    events = await reader.read_for_place(place_id)

    if not events:
        print(f"No evidence recorded for place_id={place_id}")
        return 0

    print(f"place_id: {place_id}")
    if events:
        print(f"place_name: {events[0].get('place_name')}")
        print(f"provider_id: {events[0].get('provider_id')}")
    print(f"events: {len(events)}")
    print()

    if raw:
        for ev in events:
            print(json.dumps(ev, indent=2, ensure_ascii=False))
            print("---")
        return 0

    # Compact summary: per-event one-liner + an aggregated producer tally.
    producer_counts: Counter[str] = Counter()
    for ev in events:
        recorded = ev.get("recorded_at", "?")
        url = ev.get("source_url") or "(no url)"
        user = ev.get("user_id", "?")
        evidence_items = ev.get("evidence", [])
        producers = [e.get("producer", "?") for e in evidence_items]
        producer_counts.update(producers)
        print(f"  {recorded}  user={user}  {len(evidence_items)} evidence")
        print(f"    source: {url}")
        print(f"    producers: {', '.join(producers)}")
        snippets = [e.get("snippet") for e in evidence_items if e.get("snippet")]
        for sn in snippets[:2]:  # first 2 snippets per event
            print(f"    snippet: {sn!r}")
        print()

    print("Aggregate producer counts across all events:")
    for producer, count in producer_counts.most_common():
        print(f"  {producer:>20} : {count}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("place_id", help="UUID of the place to inspect")
    parser.add_argument(
        "--raw", action="store_true", help="Dump full JSON for each event"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.place_id, args.raw)))
