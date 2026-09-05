"""Integration tests spanning multiple parsers + the knowledge graph.

The graph-building path runs without external services. The full pipeline test
(which writes to Qdrant) is skipped automatically when Qdrant is unreachable.
"""
from __future__ import annotations

import pytest

from mcp_kb.graph.knowledge_graph import KnowledgeGraph
from mcp_kb.ingestion.parsers import ParserRegistry
from mcp_kb.ingestion.repo_scanner import RepoScanner
from mcp_kb.models import NodeType, ParseResult

from ..conftest import qdrant_available


def _build_graph(settings, fixture_repo) -> KnowledgeGraph:
    scanner = RepoScanner(settings)
    parsers = ParserRegistry(settings)
    aggregate = ParseResult()
    for source in scanner.scan("rx-order-service", fixture_repo):
        aggregate.extend(parsers.parse(source))
    graph = KnowledgeGraph(settings)
    graph.add_many(aggregate.nodes, aggregate.edges)
    return graph


def test_end_to_end_graph_construction(settings, fixture_repo):
    graph = _build_graph(settings, fixture_repo)
    stats = graph.stats()
    assert "rx-order-service" in graph.services()

    # All major node types are present from the mixed-content repo.
    for ntype in (NodeType.SERVICE, NodeType.CONTROLLER, NodeType.ENDPOINT,
                  NodeType.ENTITY, NodeType.TABLE, NodeType.REPOSITORY,
                  NodeType.TOPIC, NodeType.INCIDENT):
        assert graph.nodes_by_type(ntype), f"missing {ntype}"

    # Endpoints discovered from the controller.
    endpoints = graph.nodes_by_type(NodeType.ENDPOINT, "rx-order-service")
    assert len(endpoints) >= 4
    assert stats["edges"] > 0


def test_impact_analysis_over_real_repo(settings, fixture_repo):
    graph = _build_graph(settings, fixture_repo)
    table = graph.nodes_by_type(NodeType.TABLE, "rx-order-service")[0]
    impact = graph.impacted(table["id"], max_hops=3)
    # The orders table maps back from the Order entity (reverse reachability).
    names = {n["name"] for level in impact["levels"] for n in level}
    assert "Order" in names


@pytest.mark.skipif(not qdrant_available(), reason="Qdrant not running")
def test_full_pipeline_with_vectors(settings, fixture_repo, tmp_path):
    import shutil

    from mcp_kb.ingestion.pipeline import IngestionPipeline

    repo_dst = settings.repos_root / "rx-order-service"
    repo_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture_repo, repo_dst)
    # Make it a git repo so the pipeline can read a commit.
    from git import Repo

    r = Repo.init(repo_dst)
    r.index.add(["."])
    r.index.commit("init")

    pipeline = IngestionPipeline(settings)
    report = pipeline.ingest_repo("rx-order-service", incremental=False)
    # ingest_repo() alone does not persist the graph snapshot (only
    # ingest_all()/refresh_all() do) — mirror the same explicit save()
    # cli_main() performs for its equivalent `--repo` single-repo path.
    pipeline.graph.save()
    assert report.files_indexed > 0
    assert report.chunks_written > 0

    from mcp_kb.tools.knowledge_service import KnowledgeService

    svc = KnowledgeService(settings)
    resp = svc.find_endpoint("orders")
    assert resp.data["match_count"] >= 4
    arch = svc.generate_architecture_summary()
    assert "rx-order-service" in arch.data["services"]
