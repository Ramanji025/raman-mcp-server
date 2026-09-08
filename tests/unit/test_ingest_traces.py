"""Follow-on: tests for runtime trace ingestion (ingest_traces overlay)."""
from __future__ import annotations

from mcp_kb.models import EdgeType, GraphEdge, GraphNode, NodeType
from mcp_kb.tools.knowledge_service._platform import PlatformMixin


class _FakeGraph:
    """Minimal in-memory GraphStorePort stand-in for isolated unit testing."""

    def __init__(self, nodes: list[GraphNode]) -> None:
        self._nodes = nodes
        self.added_edges: list[GraphEdge] = []
        self.saved = False

    def nodes_by_type(self, node_type, service=None):
        return [n.model_dump(mode="python") for n in self._nodes if n.type == node_type]

    def add_many(self, nodes, edges):
        self.added_edges.extend(edges)

    def save(self):
        self.saved = True

    def services(self):
        return ["order-service"]


class _Harness(PlatformMixin):
    def __init__(self, graph):
        self.graph = graph

    def _resolve_service(self, name):
        return name


def _method_node(qualified_name: str) -> GraphNode:
    return GraphNode(id=f"method:order-service:{qualified_name}", type=NodeType.METHOD,
                     name=qualified_name.rsplit(".", 1)[-1], service="order-service",
                     attributes={"qualified_name": qualified_name})


def test_ingest_traces_resolves_known_methods_and_creates_runtime_call_edges():
    nodes = [_method_node("OrderController.placeOrder"), _method_node("OrderService.process")]
    graph = _FakeGraph(nodes)
    svc = _Harness(graph)

    resp = svc.ingest_traces("order-service", traces=[
        {"caller": "OrderController.placeOrder", "callee": "OrderService.process", "count": 42},
    ])

    assert resp.data["accepted"] == 1
    assert resp.data["skipped"] == []
    assert len(graph.added_edges) == 1
    edge = graph.added_edges[0]
    assert edge.type == EdgeType.RUNTIME_CALL
    assert edge.attributes["count"] == 42
    assert graph.saved is True


def test_ingest_traces_never_fabricates_nodes_for_unresolved_methods():
    nodes = [_method_node("OrderController.placeOrder")]
    graph = _FakeGraph(nodes)
    svc = _Harness(graph)

    resp = svc.ingest_traces("order-service", traces=[
        {"caller": "OrderController.placeOrder", "callee": "NonExistent.method", "count": 5},
    ])

    assert resp.data["accepted"] == 0
    assert len(resp.data["skipped"]) == 1
    assert resp.data["skipped"][0]["reason"] == "callee_not_found"
    assert graph.added_edges == []  # no fabricated node/edge for the unresolved callee
