"""Unit tests for the knowledge graph traversal + impact analysis."""
from __future__ import annotations

from mcp_kb.graph.knowledge_graph import KnowledgeGraph
from mcp_kb.models import EdgeType, GraphEdge, GraphNode, NodeType


def _build(settings) -> KnowledgeGraph:
    g = KnowledgeGraph(settings)
    nodes = [
        GraphNode(id="service:rx-order:rx-order", type=NodeType.SERVICE,
                  name="rx-order", service="rx-order"),
        GraphNode(id="entity:rx-order:Order", type=NodeType.ENTITY,
                  name="Order", service="rx-order", attributes={"file": "Order.java"}),
        GraphNode(id="table:rx-order:orders", type=NodeType.TABLE,
                  name="orders", service="rx-order"),
        GraphNode(id="repository:rx-order:OrderRepository", type=NodeType.REPOSITORY,
                  name="OrderRepository", service="rx-order"),
        GraphNode(id="endpoint:rx-order:GET /api/orders", type=NodeType.ENDPOINT,
                  name="GET /api/orders", service="rx-order"),
        GraphNode(id="service:rx-pricing:rx-pricing", type=NodeType.SERVICE,
                  name="rx-pricing", service="rx-pricing"),
    ]
    edges = [
        GraphEdge(src="entity:rx-order:Order", dst="table:rx-order:orders",
                  type=EdgeType.MAPS_TO),
        GraphEdge(src="repository:rx-order:OrderRepository",
                  dst="entity:rx-order:Order", type=EdgeType.QUERIES),
        GraphEdge(src="endpoint:rx-order:GET /api/orders",
                  dst="service:rx-order:rx-order", type=EdgeType.DECLARED_IN),
        GraphEdge(src="service:rx-order:rx-order",
                  dst="service:rx-pricing:rx-pricing", type=EdgeType.CALLS),
    ]
    g.add_many(nodes, edges)
    return g


def test_impact_of_table_reaches_repository(settings):
    g = _build(settings)
    impact = g.impacted("table:rx-order:orders", max_hops=3)
    impacted_ids = {n["id"] for level in impact["levels"] for n in level}
    assert "entity:rx-order:Order" in impacted_ids
    assert "repository:rx-order:OrderRepository" in impacted_ids
    assert impact["total_impacted"] >= 2


def test_service_dependency_map(settings):
    g = _build(settings)
    deps = g.service_dependency_map()
    assert deps.get("rx-order") == ["rx-pricing"]


def test_find_nodes_ranks_exact_match_first(settings):
    g = _build(settings)
    results = g.find_nodes("Order", {NodeType.ENTITY})
    assert results[0]["name"] == "Order"


def test_snapshot_roundtrip(settings):
    g = _build(settings)
    g.save()
    g2 = KnowledgeGraph(settings)
    assert g2.load()
    assert g2.stats()["nodes"] == g.stats()["nodes"]
