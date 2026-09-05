"""Migrate ``data/graph/graph.json`` to Neo4j with validation and reporting.

Usage:
    .\.venv\Scripts\python.exe scripts\migrate_graph_to_neo4j.py
    .\.venv\Scripts\python.exe scripts\migrate_graph_to_neo4j.py --dry-run

The source graph.json is never modified. A source snapshot and JSON migration
report are written to data/graph/migrations/ for each graph checksum.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_kb.graph.graph_json_to_neo4j import GraphJsonToNeo4jMigrator  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500,
                        help="nodes or relationships per Neo4j UNWIND batch")
    parser.add_argument("--dry-run", action="store_true",
                        help="load and count graph.json without connecting to Neo4j")
    args = parser.parse_args()

    report = GraphJsonToNeo4jMigrator(batch_size=args.batch_size).migrate(
        dry_run=args.dry_run
    )
    print(json.dumps(report.__dict__, indent=2))
    if not report.validation_passed:
        raise SystemExit("Migration validation failed; graph.json was preserved.")


if __name__ == "__main__":
    main()
