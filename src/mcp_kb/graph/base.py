"""Graph store abstraction (port).

This module exists so the rest of the codebase depends on a *behaviour*
contract instead of a concrete backend. ``KnowledgeGraph`` (NetworkX +
JSON snapshot) is the default, zero-infrastructure implementation.
``Neo4jGraphStore`` (see ``graph/neo4j_store.py``) implements the same
surface for enterprise scale-out. New code (tools, retriever, RCA engine)
should be written against ``GraphStorePort`` so switching
``GRAPH_BACKEND=networkx|neo4j`` is a config change, not a rewrite.

The runtime graph backend is Neo4j. ``KnowledgeGraph`` remains available only
to the one-time legacy migration utility that reads historical graph snapshots.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..models import EdgeType, GraphEdge, GraphNode, ImpactResult, NodeType


@runtime_checkable
class GraphStorePort(Protocol):
    """Behavioural contract shared by the NetworkX and Neo4j graph stores."""

    # ---- mutation ---- #
    def add_node(self, node: GraphNode) -> None:
        """Insert or upsert a single graph node."""
        ...
    def add_edge(self, edge: GraphEdge) -> None:
        """Insert or upsert a single graph edge."""
        ...
    def add_many(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> None:
        """Bulk insert/upsert a batch of nodes and edges."""
        ...
    def remove_service_file(self, repo: str, rel_path: str) -> None:
        """Remove all nodes/edges previously written for one repo file (re-ingestion cleanup)."""
        ...

    # ---- queries ---- #
    def node(self, node_id: str) -> dict | None:
        """Return the raw node dict for `node_id`, or None if not found."""
        ...
    def all_nodes(self, service: str | None = None) -> list[dict]:
        """Return every node, optionally filtered to one service."""
        ...
    def nodes_by_type(self, node_type: NodeType, service: str | None = None) -> list[dict]:
        """Return all nodes of a given type, optionally filtered to one service."""
        ...
    def services(self) -> list[str]:
        """Return the list of distinct service names in the graph."""
        ...
    def find_nodes(self, text: str, node_types: set[NodeType] | None = None,
                   limit: int = 25) -> list[dict]:
        """Full-text/substring search for nodes matching `text`, optionally restricted to given types."""
        ...
    def neighbors(self, node_id: str, edge_types: set[EdgeType] | None = None,
                  direction: str = "out") -> list[dict]:
        """Return adjacent nodes reachable via the given edge types/direction."""
        ...
    def impacted(self, node_id: str, max_hops: int = 3) -> ImpactResult:
        """Compute blast-radius impact from `node_id` up to `max_hops`."""
        ...
    def paths_between(self, src: str, dst: str, cutoff: int = 6) -> list[list[str]]:
        """Find simple paths between two nodes up to `cutoff` hops."""
        ...
    def service_dependency_map(self) -> dict[str, list[str]]:
        """Return {service: [services it depends on]} for the whole graph."""
        ...
    def find_by_file_lines(self, file_path: str, start_line: int | None = None,
                            end_line: int | None = None) -> list[dict]:
        """Return nodes whose source location overlaps the given file/line range."""
        ...
    def find_by_file(self, file_path: str) -> list[dict]:
        """Return all nodes declared in a given source file."""
        ...
    def all_files(self) -> dict[str, list[str]]:
        """Return {repo: [indexed file paths]} for the whole graph."""
        ...
    def stats(self) -> dict:
        """Return node/edge counts and other summary graph statistics."""
        ...
    def run_cypher(self, query: str, params: dict | None = None,
                    max_rows: int = 200) -> list[dict]:
        """Execute a read-only Cypher query and return up to `max_rows` rows as dicts.

        Implementations must reject write clauses (CREATE/MERGE/DELETE/SET/
        REMOVE/DROP/CALL db.* write procs) — this is an agent-facing ad-hoc
        query surface, not a mutation API.
        """
        ...

    # ---- persistence ---- #
    def save(self) -> None:
        """Persist the graph to its backing store."""
        ...
    def load(self) -> bool:
        """Load the graph from its backing store; return True if data was found."""
        ...
