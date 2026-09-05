"""Unit tests for GraphTraversalMixin (graph/neo4j_store.py) — the shared
impacted()/paths_between() algorithm used by the Neo4j backend.

Exercised here against a minimal in-memory fake implementing just
neighbors()/node(), so this validates the traversal algorithm itself without
requiring a live Neo4j connection (that requires
tests/integration/test_neo4j_migration.py-style infra, out of scope for the
unit suite).
"""
from __future__ import annotations

from mcp_kb.graph.neo4j_store import GraphTraversalMixin


class _FakeGraph(GraphTraversalMixin):
    """In-memory adjacency-list stand-in for Neo4jGraphStore."""

    def __init__(self):
        self._nodes: dict[str, dict] = {}
        self._out: dict[str, list[tuple[str, str]]] = {}  # id -> [(edge_type, dst_id)]
        self._in: dict[str, list[tuple[str, str]]] = {}   # id -> [(edge_type, src_id)]

    def add_node(self, node_id: str, **attrs) -> None:
        self._nodes[node_id] = {"id": node_id, **attrs}
        self._out.setdefault(node_id, [])
        self._in.setdefault(node_id, [])

    def add_edge(self, src: str, edge_type: str, dst: str) -> None:
        self._out[src].append((edge_type, dst))
        self._in[dst].append((edge_type, src))

    def node(self, node_id: str) -> dict | None:
        return self._nodes.get(node_id)

    def neighbors(self, node_id: str, edge_types=None, direction: str = "out") -> list[dict]:
        table = self._out if direction == "out" else self._in
        out = []
        for edge_type, other_id in table.get(node_id, []):
            out.append({"edge_type": edge_type, "direction": direction,
                       "node": self.node(other_id)})
        return out


def _build_chain() -> _FakeGraph:
    """table <- entity <- repository <- method <- endpoint (reverse chain)."""
    g = _FakeGraph()
    for nid, service in (("table:orders", "rx-order"), ("entity:Order", "rx-order"),
                         ("repository:OrderRepository", "rx-order"),
                         ("method:OrderService.validateOrder", "rx-order"),
                         ("endpoint:POST /orders", "rx-order")):
        g.add_node(nid, service=service, type="X", name=nid)
    g.add_edge("entity:Order", "MAPS_TO", "table:orders")
    g.add_edge("repository:OrderRepository", "QUERIES", "entity:Order")
    g.add_edge("method:OrderService.validateOrder", "DELEGATES_TO", "repository:OrderRepository")
    g.add_edge("endpoint:POST /orders", "CALLS", "method:OrderService.validateOrder")
    return g


def test_impacted_reverse_reachability_finds_all_upstream_dependents():
    g = _build_chain()
    impact = g.impacted("table:orders", max_hops=4)
    impacted_ids = {n["id"] for level in impact["levels"] for n in level}
    assert "entity:Order" in impacted_ids
    assert "repository:OrderRepository" in impacted_ids
    assert "method:OrderService.validateOrder" in impacted_ids
    assert "endpoint:POST /orders" in impacted_ids
    assert impact["total_impacted"] == 4


def test_impacted_respects_max_hops_cutoff():
    g = _build_chain()
    impact = g.impacted("table:orders", max_hops=1)
    impacted_ids = {n["id"] for level in impact["levels"] for n in level}
    assert impacted_ids == {"entity:Order"}


def test_impacted_on_unknown_node_returns_empty():
    g = _build_chain()
    impact = g.impacted("table:does-not-exist", max_hops=3)
    assert impact["levels"] == []
    assert impact["services"] == []


def test_paths_between_finds_forward_path():
    g = _build_chain()
    paths = g.paths_between("endpoint:POST /orders", "table:orders", cutoff=6)
    assert paths
    assert paths[0][0] == "endpoint:POST /orders"
    assert paths[0][-1] == "table:orders"
    assert "repository:OrderRepository" in paths[0]


def test_paths_between_no_path_returns_empty_list():
    g = _build_chain()
    g.add_node("island:isolated", service="rx-other", type="X", name="isolated")
    paths = g.paths_between("endpoint:POST /orders", "island:isolated", cutoff=6)
    assert paths == []
