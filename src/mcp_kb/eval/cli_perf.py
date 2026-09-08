"""CLI: run performance benchmarks and gate on regression vs. the last run.

    python -m mcp_kb.eval.cli_perf

Always runs the infra-independent parse-throughput benchmark against the
bundled fixture repo (tests/fixtures/rx-order-service) and gates on it —
matching codebase-memory-mcp's own release policy: a >15% unexplained
slowdown vs. the last recorded run fails the build (env override:
PERF_REGRESSION_THRESHOLD, default 0.15).

Query-latency and token-efficiency benchmarks additionally run whenever a
live Neo4j/Qdrant + an already-ingested copy of the fixture repo is
reachable; these are informational only (recorded, not gated) since not
every environment running this CLI has that infra up.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .performance import benchmark_parse_throughput, benchmark_query_latency
from .token_efficiency import benchmark_token_efficiency

_HISTORY_PATH = Path("data/eval/performance_history.jsonl")
_FIXTURE_REPO_NAME = "rx-order-service"
_FIXTURE_REPO_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / _FIXTURE_REPO_NAME
_DEFAULT_THRESHOLD = 0.15  # matches CBM's documented 15% regression-blocker policy


def _load_last_run(repo: str) -> dict | None:
    if not _HISTORY_PATH.exists():
        return None
    last = None
    for line in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("parse", {}).get("repo") == repo:
            last = record
    return last


def _append_run(record: dict) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _try_live_benchmarks(repo_name: str, repo_dir: Path) -> tuple[dict | None, dict | None]:
    """Best-effort: only runs if Neo4j/Qdrant are reachable and the repo is
    already ingested. Never raises — returns (None, None) on any failure."""
    try:
        from ..config import get_settings
        from ..tools.knowledge_service import KnowledgeService

        settings = get_settings()
        svc = KnowledgeService(settings)
        if repo_name not in svc.graph.services():
            print(f"  (skipping live benchmarks: '{repo_name}' not yet ingested into the graph)")
            return None, None
        query_latency = benchmark_query_latency(svc, repo_name)
        token_eff = benchmark_token_efficiency(svc, repo_name, repo_dir, settings)
        return query_latency, token_eff
    except Exception as exc:  # pragma: no cover - environment-dependent, must not crash CI
        print(f"  (skipping live benchmarks: {exc})")
        return None, None


def main() -> None:
    """CLI entrypoint: run performance benchmarks and gate on regression."""
    from ..config import get_settings

    settings = get_settings()
    threshold = float(os.environ.get("PERF_REGRESSION_THRESHOLD", _DEFAULT_THRESHOLD))

    print(f"Parsing fixture repo: {_FIXTURE_REPO_DIR}")
    parse_metrics = benchmark_parse_throughput(_FIXTURE_REPO_NAME, _FIXTURE_REPO_DIR, settings)
    print(f"  files={parse_metrics['files_parsed']} loc={parse_metrics['total_loc']} "
          f"nodes={parse_metrics['nodes']} edges={parse_metrics['edges']} "
          f"time={parse_metrics['seconds']}s "
          f"({parse_metrics['files_per_sec']} files/s, {parse_metrics['loc_per_sec']} loc/s)")

    query_latency, token_efficiency = _try_live_benchmarks(_FIXTURE_REPO_NAME, _FIXTURE_REPO_DIR)
    if query_latency:
        print("Query latency (p50/p95 ms):")
        for tool, stats in query_latency.items():
            print(f"  {tool}: p50={stats['p50_ms']} p95={stats['p95_ms']}")
    if token_efficiency:
        print(f"Token efficiency: baseline={token_efficiency['baseline_tokens_approx']} "
              f"structured={token_efficiency['structured_tokens_approx']} "
              f"ratio={token_efficiency['token_ratio']}x")

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "parse": parse_metrics,
        "query_latency": query_latency,
        "token_efficiency": token_efficiency,
    }

    last = _load_last_run(_FIXTURE_REPO_NAME)
    _append_run(record)

    if last is None:
        print("No prior run recorded — baseline established, nothing to compare.")
        return

    last_rate = last.get("parse", {}).get("files_per_sec")
    current_rate = parse_metrics.get("files_per_sec")
    if not last_rate or not current_rate:
        print("Prior run missing files_per_sec — skipping regression check.")
        return

    regression = (last_rate - current_rate) / last_rate
    print(f"Prior files/sec: {last_rate}  Current: {current_rate}  "
          f"Delta: {regression * 100:+.1f}%  Threshold: {threshold * 100:.0f}%")
    if regression > threshold:
        print(f"FAIL: parse throughput regressed by {regression * 100:.1f}% "
              f"(> {threshold * 100:.0f}% threshold).", file=sys.stderr)
        raise SystemExit(1)
    print("OK: no significant performance regression.")


if __name__ == "__main__":
    main()
