"""In-memory + persisted code knowledge graph.

Backed by NetworkX for traversal (impact analysis, business-flow tracing)
and persisted to a JSON snapshot so the MCP server can load it at startup
without re-ingesting. Nodes/edges are also mirrored to Postgres when enabled.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import networkx as nx

from ..config import Settings
from ..logging import get_logger
from ..models import EdgeType, GraphEdge, GraphNode, ImpactResult, NodeType

log = get_logger(__name__)


class KnowledgeGraph:
    """Directed multigraph of the microservice ecosystem."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._g = nx.MultiDiGraph()
        self._snapshot = Path(settings.repos_root).parent / "graph" / "graph.json"

    # ---- mutation ---- #
    def add_node(self, node: GraphNode) -> None:
        """Insert a node, merging attributes into any existing node with the same id."""
        existing = self._g.nodes.get(node.id)
        attrs = {**node.attributes}
        if existing:
            merged = {**existing.get("attributes", {}), **attrs}
            attrs = merged
        self._g.add_node(node.id, type=node.type.value, name=node.name,
                         service=node.service, attributes=attrs)

    def add_edge(self, edge: GraphEdge) -> None:
        """Insert a directed, typed edge between two existing node ids."""
        self._g.add_edge(edge.src, edge.dst, key=edge.type.value,
                         type=edge.type.value, attributes=edge.attributes)

    def add_many(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> None:
        """Bulk insert a batch of nodes then edges."""
        for n in nodes:
            self.add_node(n)
        for e in edges:
            self.add_edge(e)

    def remove_service_file(self, repo: str, rel_path: str) -> None:
        """Drop nodes whose provenance file matches (used on incremental delete)."""
        to_remove = [
            nid for nid, data in self._g.nodes(data=True)
            if data.get("service") == repo
            and data.get("attributes", {}).get("file") == rel_path
        ]
        self._g.remove_nodes_from(to_remove)

    # ---- queries ---- #
    def node(self, node_id: str) -> dict | None:
        """Return the node dict for `node_id`, or None if not present."""
        if node_id not in self._g:
            return None
        return {"id": node_id, **self._g.nodes[node_id]}

    def all_nodes(self, service: str | None = None) -> list[dict]:
        """Return every node, optionally filtered to one service."""
        return [
            {"id": node_id, **data}
            for node_id, data in self._g.nodes(data=True)
            if service is None or data.get("service") == service
        ]

    def nodes_by_type(self, node_type: NodeType, service: str | None = None) -> list[dict]:
        """Return all nodes of a given type, optionally filtered to one service."""
        out = []
        for nid, data in self._g.nodes(data=True):
            if data.get("type") != node_type.value:
                continue
            if service and data.get("service") != service:
                continue
            out.append({"id": nid, **data})
        return out

    def services(self) -> list[str]:
        """Return the sorted list of distinct service names in the graph."""
        return sorted({
            data.get("service") for _, data in self._g.nodes(data=True)
            if data.get("service")
        })

    def find_nodes(self, text: str, node_types: set[NodeType] | None = None,
                   limit: int = 25) -> list[dict]:
        """Ranked substring search for nodes matching `text`, optionally restricted to given types."""
        needle = text.lower()
        types = {t.value for t in node_types} if node_types else None
        scored: list[tuple[int, dict]] = []
        for nid, data in self._g.nodes(data=True):
            if types and data.get("type") not in types:
                continue
            name = (data.get("name") or "").lower()
            attrs = json.dumps(data.get("attributes", {})).lower()
            score = 0
            if needle == name:
                score = 100
            elif needle in name:
                score = 60
            elif needle in attrs:
                score = 30
            if score:
                scored.append((score, {"id": nid, **data}))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:limit]]

    def neighbors(self, node_id: str, edge_types: set[EdgeType] | None = None,
                  direction: str = "out") -> list[dict]:
        """Return adjacent nodes reachable via the given edge types/direction."""
        if node_id not in self._g:
            return []
        want = {e.value for e in edge_types} if edge_types else None
        out: list[dict] = []
        iterator = (
            self._g.out_edges(node_id, keys=True, data=True) if direction == "out"
            else self._g.in_edges(node_id, keys=True, data=True)
        )
        for src, dst, key, data in iterator:
            if want and key not in want:
                continue
            other = dst if direction == "out" else src
            out.append({
                "edge_type": key,
                "direction": direction,
                "node": self.node(other),
                "attributes": data.get("attributes", {}),
            })
        return out

    def impacted(self, node_id: str, max_hops: int = 3) -> ImpactResult:
        """Reverse-reachability: everything that depends on ``node_id``."""
        if node_id not in self._g:
            return {"root": node_id, "levels": [], "services": []}
        reverse = self._g.reverse(copy=False)
        levels: list[list[dict]] = []
        seen = {node_id}
        frontier = [node_id]
        for _ in range(max_hops):
            nxt: list[dict] = []
            new_frontier: list[str] = []
            for nid in frontier:
                for _, neighbor, key in reverse.out_edges(nid, keys=True):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    new_frontier.append(neighbor)
                    nxt.append({"via": key, **(self.node(neighbor) or {"id": neighbor})})
            if not nxt:
                break
            levels.append(nxt)
            frontier = new_frontier
        services = sorted({
            n.get("service") for lvl in levels for n in lvl if n.get("service")
        })
        return {"root": node_id, "levels": levels, "services": services,
                "total_impacted": len(seen) - 1}

    def paths_between(self, src: str, dst: str, cutoff: int = 6) -> list[list[str]]:
        """Return up to 10 simple paths between two nodes, at most `cutoff` hops long."""
        if src not in self._g or dst not in self._g:
            return []
        try:
            simple = nx.all_simple_paths(self._g, src, dst, cutoff=cutoff)
            return [p for _, p in zip(range(10), simple)]
        except nx.NetworkXNoPath:
            return []

    def service_dependency_map(self) -> dict[str, list[str]]:
        """Return {service: [services it calls/depends on]} derived from CALLS/DEPENDS_ON edges."""
        deps: dict[str, set[str]] = defaultdict(set)
        for src, dst, key in self._g.edges(keys=True):
            if key not in (EdgeType.CALLS.value, EdgeType.DEPENDS_ON.value):
                continue
            s_svc = self._g.nodes[src].get("service")
            d_svc = self._g.nodes[dst].get("service")
            if s_svc and d_svc and s_svc != d_svc:
                deps[s_svc].add(d_svc)
        return {k: sorted(v) for k, v in deps.items()}

    def find_by_file_lines(
        self, file_path: str, start_line: int | None = None, end_line: int | None = None,
    ) -> list[dict]:
        """Return all nodes whose provenance file matches and line range overlaps.

        Accepts basename, relative path, or path suffix — tolerant matching.
        """
        needle = file_path.replace("\\", "/").lstrip("./")
        matches: list[dict] = []
        for nid, data in self._g.nodes(data=True):
            attrs = data.get("attributes", {})
            node_file = (attrs.get("file") or "").replace("\\", "/").lstrip("./")
            # Accept exact match, suffix match, or basename match.
            if not (node_file == needle or
                    node_file.endswith(needle) or
                    needle.endswith(node_file) or
                    node_file.endswith("/" + file_path) or
                    node_file.split("/")[-1] == file_path.split("/")[-1]):
                continue
            if start_line is not None and end_line is not None:
                n_start = attrs.get("start_line")
                n_end   = attrs.get("end_line")
                if n_start is not None and n_end is not None:
                    # Overlap check: ranges intersect if neither is entirely before the other.
                    if n_end < start_line or n_start > end_line:
                        continue
            matches.append({"id": nid, **data})
        return matches

    def find_by_file(self, file_path: str) -> list[dict]:
        """Return all nodes from a given file."""
        return self.find_by_file_lines(file_path)

    def all_files(self) -> dict[str, list[str]]:
        """Return {file_path: [node_id, ...]} index for all indexed files."""
        index: dict[str, list[str]] = defaultdict(list)
        for nid, data in self._g.nodes(data=True):
            f = data.get("attributes", {}).get("file")
            if f:
                index[f].append(nid)
        return dict(index)

    def stats(self) -> dict:
        """Return node/edge counts, service count, and a per-type node breakdown."""
        by_type: dict[str, int] = defaultdict(int)
        for _, data in self._g.nodes(data=True):
            by_type[data.get("type", "?")] += 1
        return {
            "nodes": self._g.number_of_nodes(),
            "edges": self._g.number_of_edges(),
            "services": len(self.services()),
            "by_type": dict(by_type),
        }

    def run_cypher(self, query: str, params: dict | None = None,
                    max_rows: int = 200) -> list[dict]:
        """Not supported on the legacy NetworkX backend — use GRAPH_BACKEND=neo4j."""
        raise NotImplementedError(
            "query_graph requires GRAPH_BACKEND=neo4j (legacy NetworkX store has no Cypher engine)"
        )

    # ---- persistence ---- #
    def save(self) -> None:
        """Atomically write the graph to its JSON snapshot file and rotate backups."""
        self._snapshot.parent.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(self._g, edges="links")
        payload = json.dumps(data)
        # P3.3: write to a temp file then atomically rename so a crash mid-write
        # never leaves a corrupt snapshot. Keep the last 3 versioned backups.
        tmp = self._snapshot.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._snapshot)
        self._rotate_backups(keep=3)
        log.info("graph_saved", path=str(self._snapshot), **self.stats())

    def _rotate_backups(self, keep: int = 3) -> None:
        """Keep the N most-recent timestamped backup copies."""
        import time
        ts = int(time.time())
        backup = self._snapshot.with_suffix(f".bak{ts}.json")
        try:
            import shutil
            shutil.copy2(self._snapshot, backup)
        except OSError as exc:
            log.debug("graph_backup_copy_failed", error=str(exc))
            return
        pattern = self._snapshot.stem
        backups = sorted(
            self._snapshot.parent.glob(f"{pattern}.bak*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in backups[:-keep]:
            try:
                old.unlink()
            except OSError as exc:
                log.debug("graph_backup_prune_failed", path=str(old), error=str(exc))

    def load(self) -> bool:
        """Load the graph from its JSON snapshot file; return True if data was found."""
        if not self._snapshot.exists():
            return False
        raw = self._snapshot.read_text(encoding="utf-8").strip()
        if not raw:
            log.warning("graph_snapshot_empty", path=str(self._snapshot))
            return False
        data = json.loads(raw)
        self._g = nx.node_link_graph(data, multigraph=True, directed=True, edges="links")
        log.info("graph_loaded", path=str(self._snapshot), **self.stats())
        return True

    @property
    def raw(self) -> nx.MultiDiGraph:
        """The underlying networkx MultiDiGraph, for callers that need direct access."""
        return self._g
