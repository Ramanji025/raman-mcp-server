"""Phase 5: tests for the performance/token-efficiency benchmark modules."""
from __future__ import annotations

from mcp_kb.eval.performance import benchmark_parse_throughput
from mcp_kb.eval.token_efficiency import _approx_tokens


def test_benchmark_parse_throughput_returns_sane_metrics(settings, fixture_repo):
    metrics = benchmark_parse_throughput("rx-order-service", fixture_repo, settings)

    assert metrics["repo"] == "rx-order-service"
    assert metrics["files_parsed"] > 0
    assert metrics["total_loc"] > 0
    assert metrics["seconds"] >= 0
    assert metrics["files_per_sec"] is not None
    assert metrics["files_per_sec"] > 0


def test_benchmark_parse_throughput_is_deterministic_in_node_edge_counts(settings, fixture_repo):
    """Same repo parsed twice must yield identical node/edge counts — this is
    the invariant the CI regression gate relies on for a stable baseline."""
    first = benchmark_parse_throughput("rx-order-service", fixture_repo, settings)
    second = benchmark_parse_throughput("rx-order-service", fixture_repo, settings)

    assert first["files_parsed"] == second["files_parsed"]
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]


def test_approx_tokens_heuristic():
    assert _approx_tokens("") == 1  # never zero — avoids div-by-zero downstream
    assert _approx_tokens("a" * 400) == 100
