"""Unit tests for graph backend selection (graph/factory.py)."""
from __future__ import annotations

import pytest

from mcp_kb.graph.factory import get_graph_store
from mcp_kb.graph.knowledge_graph import KnowledgeGraph


def test_networkx_is_a_supported_zero_infrastructure_backend(settings):
    """Phase 7: NetworkX is an officially-supported local mode, not rejected."""
    settings.graph_backend = "networkx"
    store = get_graph_store(settings)
    assert isinstance(store, KnowledgeGraph)


def test_unknown_runtime_backend_is_rejected(settings):
    settings.graph_backend = "not-a-real-backend"
    with pytest.raises(ValueError, match="Unsupported GRAPH_BACKEND"):
        get_graph_store(settings)


def test_neo4j_backend_selection_raises_when_unreachable(settings):
    """Neo4j must fail fast rather than silently creating an empty graph."""
    settings.graph_backend = "neo4j"
    settings.neo4j_uri = "bolt://localhost:1"  # nothing listens here
    raised = False
    try:
        get_graph_store(settings)
    except Exception:
        raised = True
    assert raised, "expected a connection error instead of a silent empty graph"
