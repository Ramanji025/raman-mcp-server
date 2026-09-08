"""Phase 5: performance benchmarking (parse throughput + query latency).

Mirrors codebase-memory-mcp's benchmark methodology (docs/BENCHMARK.md,
MAINTAINERS.md release checklist): measure a repeatable metric, compare
against the last recorded run, and treat >15% unexplained regression as a
release blocker.

Two independent benchmarks, split deliberately:

- `benchmark_parse_throughput`: pure parse-stage speed (RepoScanner +
  ParserRegistry, no graph/vector writes). Infra-independent — runs the same
  way in CI as on a laptop, no Neo4j/Qdrant required. This is the metric
  the CI regression gate enforces (see eval/cli_perf.py).
- `benchmark_query_latency`: p50/p95 latency for a fixed set of
  representative MCP tool calls against a *live* KnowledgeService. Requires
  Neo4j/Qdrant to be reachable; best-effort/informational only — not gated,
  since not every CI run has infra available (matches CBM's own separation
  of "index time" from "quality"/"token" measurements into independent,
  separately-reported numbers rather than one blended score).
"""
from __future__ import annotations

import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from ..config import Settings
from ..ingestion.parsers import ParserRegistry
from ..ingestion.repo_scanner import RepoScanner
from ..logging import get_logger

log = get_logger(__name__)


def benchmark_parse_throughput(repo_name: str, repo_dir: Path, settings: Settings) -> dict[str, Any]:
    """Parse every ingestible file in `repo_dir` and return throughput metrics.

    Does not touch Neo4j/Qdrant — pure tree-sitter/parser CPU time, so this
    is safe and fast to run on every CI job.
    """
    scanner = RepoScanner(settings)
    registry = ParserRegistry(settings)

    files_parsed = 0
    total_loc = 0
    total_nodes = 0
    total_edges = 0
    started = time.perf_counter()
    for source in scanner.scan(repo_name, repo_dir):
        result = registry.parse(source)
        files_parsed += 1
        total_nodes += len(result.nodes)
        total_edges += len(result.edges)
        try:
            total_loc += Path(source.abs_path).read_text(encoding="utf-8", errors="ignore").count("\n") + 1
        except OSError:
            pass
    elapsed = time.perf_counter() - started

    return {
        "repo": repo_name,
        "files_parsed": files_parsed,
        "total_loc": total_loc,
        "nodes": total_nodes,
        "edges": total_edges,
        "seconds": round(elapsed, 4),
        "files_per_sec": round(files_parsed / elapsed, 2) if elapsed > 0 else None,
        "loc_per_sec": round(total_loc / elapsed, 1) if elapsed > 0 else None,
    }


# ---- Representative query set for latency benchmarking ---- #
# Kept small and fast so this can run inside a CI job's time budget; not
# meant to be an exhaustive coverage suite (the golden-question eval in
# eval/golden.py already covers answer quality).
def benchmark_query_latency(knowledge_service: Any, service_name: str,
                            repeats: int = 5) -> dict[str, Any]:
    """Time a fixed set of representative tool calls; returns p50/p95/mean per tool.

    `knowledge_service` is a live `KnowledgeService` instance (already
    connected to Neo4j/Qdrant) — callers are responsible for confirming
    connectivity before calling this (see eval/cli_perf.py's try/except).
    """
    calls: dict[str, Callable[[], Any]] = {
        "list_indexed_files": lambda: knowledge_service.list_indexed_files(service_name),
        "generate_architecture_summary": lambda: knowledge_service.generate_architecture_summary(),
        "explain_service": lambda: knowledge_service.explain_service(service_name),
        "search_domain_knowledge": lambda: knowledge_service.search_domain_knowledge("order status"),
    }
    results: dict[str, Any] = {}
    for name, fn in calls.items():
        durations_ms: list[float] = []
        for _ in range(repeats):
            started = time.perf_counter()
            try:
                fn()
            except Exception as exc:  # pragma: no cover - informational benchmark, must not crash CI
                log.warning("benchmark_query_failed", tool=name, error=str(exc))
                durations_ms = []
                break
            durations_ms.append((time.perf_counter() - started) * 1000)
        if durations_ms:
            sorted_ms = sorted(durations_ms)
            results[name] = {
                "p50_ms": round(sorted_ms[len(sorted_ms) // 2], 2),
                "p95_ms": round(sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * 0.95))], 2),
                "mean_ms": round(mean(durations_ms), 2),
                "samples": len(durations_ms),
            }
    return results
