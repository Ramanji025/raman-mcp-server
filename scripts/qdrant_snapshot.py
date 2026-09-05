"""P5.1: Qdrant snapshot job — call once per night (e.g. Task Scheduler / cron).

Usage:
    python scripts/qdrant_snapshot.py
    python scripts/qdrant_snapshot.py --keep 5 --url http://localhost:6333

Creates a snapshot for every mcpkb_* collection and deletes old ones, keeping
only the N most recent per collection.  Safe to run while the server is live.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import urllib.request as _req
except ImportError:
    _req = None  # type: ignore[assignment]


def _get(url: str) -> Any:
    with _req.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def _post(url: str) -> Any:
    req = _req.Request(url, data=b"", method="POST")
    with _req.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _delete(url: str) -> None:
    req = _req.Request(url, method="DELETE")
    with _req.urlopen(req, timeout=10):
        pass


def snapshot_all(base_url: str, prefix: str, keep: int) -> None:
    collections = _get(f"{base_url}/collections")["result"]["collections"]
    targets = [c["name"] for c in collections if c["name"].startswith(prefix)]
    if not targets:
        print(f"No collections found with prefix '{prefix}'")
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for col in targets:
        # create snapshot
        result = _post(f"{base_url}/collections/{col}/snapshots")
        snap_name = result.get("result", {}).get("name", "")
        print(f"[{col}] snapshot created: {snap_name}")
        # list all snapshots and prune oldest beyond keep
        all_snaps = _get(f"{base_url}/collections/{col}/snapshots")["result"]
        if len(all_snaps) > keep:
            to_delete = sorted(all_snaps, key=lambda s: s.get("creation_time", ""))[: len(all_snaps) - keep]
            for old in to_delete:
                _delete(f"{base_url}/collections/{col}/snapshots/{old['name']}")
                print(f"[{col}] deleted old snapshot: {old['name']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Qdrant snapshot job")
    parser.add_argument("--url", default="http://localhost:6333")
    parser.add_argument("--prefix", default="mcpkb")
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()
    try:
        snapshot_all(args.url, args.prefix, args.keep)
        print("Snapshot job complete.")
    except Exception as exc:
        print(f"Snapshot job failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
