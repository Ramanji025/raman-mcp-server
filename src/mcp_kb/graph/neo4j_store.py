"""Neo4j-backed graph store — enterprise scale-out alternative to NetworkX.

Implements the same duck-typed surface as
:class:`mcp_kb.graph.knowledge_graph.KnowledgeGraph` (see
``graph/base.py::GraphStorePort``) so callers (the hybrid retriever, the
RCA engine, and any *new* MCP tool) work unmodified regardless of which
backend ``GRAPH_BACKEND`` selects.

Design notes
------------
* Every node gets a generic ``:KBNode`` label (for uniform querying) plus a
  dynamic label matching its :class:`~mcp_kb.models.NodeType` value, e.g.
  ``(:KBNode:Service {id: "..."})``. Labels are restricted to the closed
  ``NodeType``/``EdgeType`` enums before being interpolated into Cypher, so
  this is not susceptible to Cypher injection from ingested content.
* Free-form ``attributes`` are stored twice: as a JSON blob
  (``attributes_json``) for full fidelity, and the handful of hot fields
  used by traversal/search (``file``, ``start_line``, ``end_line``,
  ``class``) are promoted to top-level indexed properties for fast lookups
  at scale (millions of nodes).
* Multi-hop algorithms (``impacted``, ``paths_between``) are implemented in
  pure Python on top of the primitive ``neighbors()`` call rather than as
  bespoke Cypher, so their semantics are guaranteed identical to the
  NetworkX backend (same algorithm, different node/edge source) and stay
  easy to unit test with a fake in-memory port.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ..config import Settings
from ..logging import get_logger
from ..models import EdgeType, GraphEdge, GraphNode, ImpactResult, NodeType

log = get_logger(__name__)

_PROMOTED_ATTRS = ("file", "start_line", "end_line", "class", "llm_summary", "llm_enriched")
_PROMOTED_EDGE_ATTRS = ("seq", "via", "role", "step")

_VALID_LABELS = {t.value for t in NodeType}
_VALID_REL_TYPES = {e.value for e in EdgeType}


class GraphTraversalMixin:
    """Backend-agnostic multi-hop algorithms built on ``neighbors()``.

    Any class providing ``neighbors(node_id, edge_types, direction)`` and
    ``node(node_id)`` gets ``impacted`` and ``paths_between`` for free, with
    behaviour identical to :class:`KnowledgeGraph`.
    """

    def neighbors(self, node_id: str, edge_types=None, direction: str = "out") -> list[dict]:
        """Abstract: return adjacent nodes; must be implemented by the concrete store."""
        raise NotImplementedError

    def node(self, node_id: str) -> dict | None:
        """Abstract: return the node dict for `node_id`; must be implemented by the concrete store."""
        raise NotImplementedError

    def impacted(self, node_id: str, max_hops: int = 3) -> ImpactResult:
        """Reverse-reachability: everything that depends on `node_id`, via `neighbors`."""
        root = self.node(node_id)
        if root is None:
            return {"root": node_id, "levels": [], "services": []}
        levels: list[list[dict]] = []
        seen = {node_id}
        frontier = [node_id]
        for _ in range(max_hops):
            nxt: list[dict] = []
            new_frontier: list[str] = []
            for nid in frontier:
                for nb in self.neighbors(nid, direction="in"):
                    n = nb.get("node")
                    if not n or n["id"] in seen:
                        continue
                    seen.add(n["id"])
                    new_frontier.append(n["id"])
                    nxt.append({"via": nb["edge_type"], **n})
            if not nxt:
                break
            levels.append(nxt)
            frontier = new_frontier
        services = sorted({n.get("service") for lvl in levels for n in lvl if n.get("service")})
        return {"root": node_id, "levels": levels, "services": services,
                "total_impacted": len(seen) - 1}

    def paths_between(self, src: str, dst: str, cutoff: int = 6) -> list[list[str]]:
        """Return up to 10 simple paths between two nodes, at most `cutoff` hops long."""
        if self.node(src) is None or self.node(dst) is None:
            return []
        paths: list[list[str]] = []

        def dfs(current: str, target: str, path: list[str], visited: set[str]) -> None:
            if len(paths) >= 10 or len(path) > cutoff:
                return
            if current == target:
                paths.append(list(path))
                return
            for nb in self.neighbors(current, direction="out"):
                n = nb.get("node")
                if not n or n["id"] in visited:
                    continue
                visited.add(n["id"])
                path.append(n["id"])
                dfs(n["id"], target, path, visited)
                path.pop()
                visited.discard(n["id"])

        dfs(src, dst, [src], {src})
        return paths


def _driver_notification_kwargs() -> dict[str, Any]:
    """Hide Neo4j INFORMATION notes such as CREATE … IF NOT EXISTS no-ops.

    Neo4j 5 still emits GQL status 00NA0 when a constraint/index already
    exists. The Python driver turns those into stderr warnings unless the
    minimum severity is WARNING. ``GraphDatabase.driver`` accepts these via a
    ``**config`` catch-all, so ``inspect.signature`` can't be used to detect
    support — pass them unconditionally and fall back if the installed
    driver rejects them.
    """
    return {"notifications_min_severity": "WARNING"}


class Neo4jGraphStore(GraphTraversalMixin):
    """Neo4j-backed implementation of :class:`~mcp_kb.graph.base.GraphStorePort`."""

    def __init__(self, settings: Settings) -> None:
        from neo4j import GraphDatabase  # optional dependency, imported lazily

        self._settings = settings
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=settings.neo4j_auth,
            **_driver_notification_kwargs(),
        )
        self._database = settings.neo4j_database
        # Fail fast and loudly if Neo4j is unreachable — silent fallback to an
        # empty graph would be far worse than an explicit connection error.
        self._driver.verify_connectivity()
        self.ensure_schema()

    def close(self) -> None:
        """Close the underlying Neo4j driver connection."""
        self._driver.close()

    # ---- schema ---- #
    def ensure_schema(self) -> None:
        """Create the KBNode uniqueness constraint and lookup indexes if missing."""
        stmts = [
            "CREATE CONSTRAINT kbnode_id IF NOT EXISTS FOR (n:KBNode) REQUIRE n.id IS UNIQUE",
            "CREATE INDEX kbnode_type IF NOT EXISTS FOR (n:KBNode) ON (n.type)",
            "CREATE INDEX kbnode_service IF NOT EXISTS FOR (n:KBNode) ON (n.service)",
            "CREATE INDEX kbnode_file IF NOT EXISTS FOR (n:KBNode) ON (n.file)",
            "CREATE INDEX kbnode_name IF NOT EXISTS FOR (n:KBNode) ON (n.name)",
            "CREATE INDEX kbnode_class_name IF NOT EXISTS FOR (n:KBNode) ON (n.class, n.name)",
            "CREATE INDEX kbnode_llm_enriched IF NOT EXISTS FOR (n:KBNode) ON (n.llm_enriched)",
        ]
        with self._driver.session(database=self._database) as session:
            for stmt in stmts:
                try:
                    session.run(stmt).consume()
                except Exception as exc:  # pragma: no cover - older Neo4j syntax fallback
                    log.debug("neo4j_schema_stmt_skipped", stmt=stmt, error=str(exc))

    # ---- mutation ---- #
    def add_node(self, node: GraphNode) -> None:
        """Insert or upsert a single graph node (MERGE by id)."""
        if node.type.value not in _VALID_LABELS:
            raise ValueError(f"unknown node type: {node.type}")
        with self._driver.session(database=self._database) as session:
            # preserve fields (e.g. llm_* enrichment) missing from this write but present before
            existing = session.run(
                "MATCH (n:KBNode {id: $id}) RETURN n.attributes_json AS attrs", id=node.id
            ).single()
            merged_attrs = dict(node.attributes)
            if existing and existing["attrs"]:
                old_attrs = json.loads(existing["attrs"])
                merged_attrs = {**old_attrs, **node.attributes}

            promoted = {k: merged_attrs.get(k) for k in _PROMOTED_ATTRS if k in merged_attrs}
            query = (
                f"MERGE (n:KBNode {{id: $id}}) "
                f"SET n:`{node.type.value}`, n.type = $type, n.name = $name, "
                f"n.service = $service, n.attributes_json = $attrs_json"
                + "".join(f", n.{k} = ${k}" for k in promoted)
            )
            params: dict[str, Any] = {
                "id": node.id, "type": node.type.value, "name": node.name,
                "service": node.service, "attrs_json": json.dumps(merged_attrs),
                **promoted,
            }
            session.run(query, **params)

    def add_edge(self, edge: GraphEdge) -> None:
        """Insert or upsert a single graph edge (MERGE by deterministic edge_id hash)."""
        if edge.type.value not in _VALID_REL_TYPES:
            raise ValueError(f"unknown edge type: {edge.type}")
        attrs_json = json.dumps(edge.attributes, sort_keys=True, default=str)
        edge_id = hashlib.sha256(
            f"{edge.src}\x00{edge.dst}\x00{edge.type.value}\x00{attrs_json}".encode()
        ).hexdigest()
        promoted = {key: edge.attributes[key] for key in _PROMOTED_EDGE_ATTRS if key in edge.attributes}
        query = (
            "MATCH (s:KBNode {id: $src}) "
            "WITH s "
            "MATCH (d:KBNode {id: $dst}) "
            f"MERGE (s)-[r:`{edge.type.value}` {{edge_id: $edge_id}}]->(d) "
            "SET r.attributes_json = $attrs_json"
            + "".join(f", r.{key} = ${key}" for key in promoted)
        )
        with self._driver.session(database=self._database) as session:
            session.run(query, src=edge.src, dst=edge.dst, edge_id=edge_id,
                        attrs_json=attrs_json, **promoted)

    def add_many(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> None:
        """Bulk upsert nodes/edges, batched per label/relationship type via UNWIND."""
        node_list = list(nodes)
        edge_list = list(edges)

        if node_list:
            # Group by label so the UNWIND query can use a static label
            by_label: dict[str, list[GraphNode]] = {}
            for n in node_list:
                if n.type.value not in _VALID_LABELS:
                    raise ValueError(f"unknown node type: {n.type}")
                by_label.setdefault(n.type.value, []).append(n)

            with self._driver.session(database=self._database) as session:
                for label, group in by_label.items():
                    # preserve fields (e.g. llm_* enrichment) missing from this write but present before
                    ids = [n.id for n in group]
                    existing_rows = session.run(
                        "UNWIND $ids AS id MATCH (n:KBNode {id: id}) "
                        "RETURN n.id AS id, n.attributes_json AS attrs",
                        ids=ids,
                    ).data()
                    existing_attrs = {
                        row["id"]: json.loads(row["attrs"]) for row in existing_rows if row["attrs"]
                    }

                    rows = []
                    for n in group:
                        merged_attrs = {**existing_attrs.get(n.id, {}), **n.attributes}
                        rows.append({
                            "id": n.id,
                            "type": n.type.value,
                            "name": n.name,
                            "service": n.service,
                            "attrs_json": json.dumps(merged_attrs),
                            **{k: merged_attrs.get(k) for k in _PROMOTED_ATTRS if k in merged_attrs},
                        })
                    query = (
                        f"UNWIND $rows AS row "
                        f"MERGE (n:KBNode {{id: row.id}}) "
                        f"SET n:`{label}`, n.type = row.type, n.name = row.name, "
                        f"n.service = row.service, n.attributes_json = row.attrs_json"
                    )
                    for attr in _PROMOTED_ATTRS:
                        query += f", n.{attr} = row.{attr}"
                    session.run(query, rows=rows)

        if edge_list:
            # Group by relationship type for static UNWIND labels
            by_rel: dict[str, list[dict]] = {}
            for e in edge_list:
                if e.type.value not in _VALID_REL_TYPES:
                    raise ValueError(f"unknown edge type: {e.type}")
                attrs_json = json.dumps(e.attributes, sort_keys=True, default=str)
                edge_id = hashlib.sha256(
                    f"{e.src}\x00{e.dst}\x00{e.type.value}\x00{attrs_json}".encode()
                ).hexdigest()
                row = {
                    "src": e.src, "dst": e.dst,
                    "edge_id": edge_id, "attrs_json": attrs_json,
                    **{k: e.attributes[k] for k in _PROMOTED_EDGE_ATTRS if k in e.attributes},
                }
                by_rel.setdefault(e.type.value, []).append(row)

            with self._driver.session(database=self._database) as session:
                for rel_type, rows in by_rel.items():
                    # Sequential MATCH avoids Cartesian product planner warning
                    query = (
                        f"UNWIND $rows AS row "
                        f"MATCH (s:KBNode {{id: row.src}}) "
                        f"WITH s, row "
                        f"MATCH (d:KBNode {{id: row.dst}}) "
                        f"MERGE (s)-[r:`{rel_type}` {{edge_id: row.edge_id}}]->(d) "
                        f"SET r.attributes_json = row.attrs_json"
                    )
                    for attr in _PROMOTED_EDGE_ATTRS:
                        query += f", r.{attr} = row.{attr}"
                    session.run(query, rows=rows)

    def remove_service_file(self, repo: str, rel_path: str) -> None:
        """Delete all nodes (and their relationships) previously written for one repo file."""
        query = (
            "MATCH (n:KBNode {service: $repo, file: $file}) DETACH DELETE n"
        )
        with self._driver.session(database=self._database) as session:
            session.run(query, repo=repo, file=rel_path)

    # ---- queries ---- #
    @staticmethod
    def _record_to_node(record: dict) -> dict:
        attrs = json.loads(record.get("attributes_json") or "{}")
        return {
            "id": record["id"], "type": record.get("type"), "name": record.get("name"),
            "service": record.get("service"), "attributes": attrs,
        }

    def node(self, node_id: str) -> dict | None:
        """Return the node dict for `node_id`, or None if not present."""
        query = "MATCH (n:KBNode {id: $id}) RETURN n"
        with self._driver.session(database=self._database) as session:
            rec = session.run(query, id=node_id).single()
            return self._record_to_node(dict(rec["n"])) if rec else None

    def all_nodes(self, service: str | None = None) -> list[dict]:
        """Return every node, optionally filtered to one service."""
        query = "MATCH (n:KBNode) "
        params: dict[str, Any] = {}
        if service:
            query += "WHERE n.service = $service "
            params["service"] = service
        query += "RETURN n"
        with self._driver.session(database=self._database) as session:
            return [self._record_to_node(dict(record["n"])) for record in session.run(query, **params)]

    def nodes_by_type(self, node_type: NodeType, service: str | None = None) -> list[dict]:
        """Return all nodes of a given type, optionally filtered to one service."""
        query = "MATCH (n:KBNode {type: $type}) "
        params: dict[str, Any] = {"type": node_type.value}
        if service:
            query += "WHERE n.service = $service "
            params["service"] = service
        query += "RETURN n"
        with self._driver.session(database=self._database) as session:
            return [self._record_to_node(dict(r["n"])) for r in session.run(query, **params)]

    def services(self) -> list[str]:
        """Return the sorted list of distinct service names in the graph."""
        query = "MATCH (n:KBNode) WHERE n.service IS NOT NULL RETURN DISTINCT n.service AS s"
        with self._driver.session(database=self._database) as session:
            return sorted(r["s"] for r in session.run(query))

    def find_nodes(self, text: str, node_types: set[NodeType] | None = None,
                   limit: int = 25) -> list[dict]:
        """Ranked substring search for nodes matching `text`, optionally restricted to given types."""
        needle = text.lower()
        query = "MATCH (n:KBNode) "
        params: dict[str, Any] = {"needle": needle}
        if node_types:
            query += "WHERE n.type IN $types "
            params["types"] = [t.value for t in node_types]
        query += (
            "WITH n, toLower(n.name) AS name, toLower(coalesce(n.attributes_json, '')) AS attrs "
            "WITH n, CASE WHEN name = $needle THEN 100 "
            "WHEN name CONTAINS $needle THEN 60 "
            "WHEN attrs CONTAINS $needle THEN 30 ELSE 0 END AS score "
            "WHERE score > 0 "
            "RETURN n, score ORDER BY score DESC LIMIT $limit"
        )
        params["limit"] = limit
        with self._driver.session(database=self._database) as session:
            return [self._record_to_node(dict(r["n"])) for r in session.run(query, **params)]

    def neighbors(self, node_id: str, edge_types: set[EdgeType] | None = None,
                  direction: str = "out") -> list[dict]:
        """Return adjacent nodes reachable via the given edge types/direction."""
        rel_filter = ""
        if edge_types:
            rel_filter = ":" + "|".join(f"`{e.value}`" for e in edge_types)
        if direction == "out":
            pattern = f"(n:KBNode {{id: $id}})-[r{rel_filter}]->(m:KBNode)"
        else:
            pattern = f"(n:KBNode {{id: $id}})<-[r{rel_filter}]-(m:KBNode)"
        query = f"MATCH {pattern} RETURN type(r) AS edge_type, r.attributes_json AS r_attrs, m"
        with self._driver.session(database=self._database) as session:
            out = []
            for rec in session.run(query, id=node_id):
                out.append({
                    "edge_type": rec["edge_type"], "direction": direction,
                    "node": self._record_to_node(dict(rec["m"])),
                    "attributes": json.loads(rec["r_attrs"] or "{}"),
                })
            return out

    def service_dependency_map(self) -> dict[str, list[str]]:
        """Return {service: [services it calls/depends on]} derived from CALLS/DEPENDS_ON edges."""
        query = (
            "MATCH (s:KBNode)-[r]->(d:KBNode) "
            "WHERE type(r) IN $types AND s.service IS NOT NULL AND d.service IS NOT NULL "
            "AND s.service <> d.service "
            "RETURN DISTINCT s.service AS src, d.service AS dst"
        )
        types = [EdgeType.CALLS.value, EdgeType.DEPENDS_ON.value]
        deps: dict[str, set[str]] = defaultdict(set)
        with self._driver.session(database=self._database) as session:
            for rec in session.run(query, types=types):
                deps[rec["src"]].add(rec["dst"])
        return {k: sorted(v) for k, v in deps.items()}

    def find_by_file_lines(self, file_path: str, start_line: int | None = None,
                            end_line: int | None = None) -> list[dict]:
        """Return nodes whose source file matches and line range overlaps."""
        needle = file_path.replace("\\", "/").lstrip("./")
        query = (
            "MATCH (n:KBNode) WHERE n.file IS NOT NULL AND "
            "(n.file = $needle OR n.file ENDS WITH $needle OR $needle ENDS WITH n.file) "
            "RETURN n"
        )
        with self._driver.session(database=self._database) as session:
            candidates = [self._record_to_node(dict(r["n"])) for r in
                          session.run(query, needle=needle)]
        if start_line is None or end_line is None:
            return candidates
        out = []
        for c in candidates:
            n_start = c["attributes"].get("start_line")
            n_end = c["attributes"].get("end_line")
            if n_start is not None and n_end is not None:
                if n_end < start_line or n_start > end_line:
                    continue
            out.append(c)
        return out

    def find_by_file(self, file_path: str) -> list[dict]:
        """Return all nodes declared in a given source file."""
        return self.find_by_file_lines(file_path)

    def all_files(self) -> dict[str, list[str]]:
        """Return {file_path: [node_id, ...]} for all indexed files."""
        query = "MATCH (n:KBNode) WHERE n.file IS NOT NULL RETURN n.file AS f, collect(n.id) AS ids"
        with self._driver.session(database=self._database) as session:
            return {r["f"]: r["ids"] for r in session.run(query)}

    def stats(self) -> dict:
        """Return node/edge counts, service count, and a per-type node breakdown."""
        with self._driver.session(database=self._database) as session:
            nodes = session.run("MATCH (n:KBNode) RETURN count(n) AS c").single()["c"]
            edges = session.run("MATCH (:KBNode)-[r]->(:KBNode) RETURN count(r) AS c").single()["c"]
            by_type = {r["t"]: r["c"] for r in session.run(
                "MATCH (n:KBNode) RETURN n.type AS t, count(n) AS c")}
        return {"nodes": nodes, "edges": edges, "services": len(self.services()),
                "by_type": by_type, "backend": "neo4j"}

    # ---- persistence: Neo4j persists automatically, these are no-ops ---- #
    def save(self) -> None:
        """No-op: Neo4j persists every write immediately."""
        log.debug("neo4j_save_noop", hint="Neo4j persists writes immediately")

    def load(self) -> bool:
        """Return True if the Neo4j connection is reachable (no explicit load needed)."""
        try:
            with self._driver.session(database=self._database) as session:
                session.run("RETURN 1").single()
            return True
        except Exception as exc:  # pragma: no cover
            log.warning("neo4j_unavailable", error=str(exc))
            return False

    @property
    def raw(self):  # pragma: no cover - parity shim, not the NetworkX graph
        """The underlying Neo4j driver (parity shim; there's no in-memory graph object here)."""
        return self._driver
