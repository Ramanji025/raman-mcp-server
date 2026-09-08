"""Portable graph snapshot export/import (Phase 3).

Mirrors codebase-memory-mcp's `.codebase-memory/graph.db.zst` team-shareable
artifact: a compressed, self-contained dump of the whole knowledge graph (or
one service) that can be committed to a repo or handed to a teammate instead
of everyone re-running the full ingestion pipeline from scratch.

Format: gzip-compressed JSON (`{"version", "exported_at", "nodes", "edges"}`).
JSON keeps the artifact readable/diffable and needs no extra dependency;
gzip already gives large (10x+) size reduction on this kind of repetitive
structured data.
"""
from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..graph.base import GraphStorePort
from ..logging import get_logger
from ..models import EdgeType, GraphEdge, GraphNode, NodeType

log = get_logger(__name__)

_SNAPSHOT_VERSION = 1


def export_snapshot(graph: GraphStorePort, path: Path, *, service: str | None = None) -> dict[str, int]:
    """Write a gzip-compressed JSON snapshot of the graph (or one service) to `path`.

    Returns {"nodes": n, "edges": m} counts written.
    """
    nodes = graph.all_nodes(service)
    node_ids = {n["id"] for n in nodes}

    if service:
        edge_query = (
            "MATCH (a:KBNode)-[r]->(b:KBNode) WHERE a.service = $service OR b.service = $service "
            "RETURN a.id AS src, b.id AS dst, type(r) AS type, r.attributes_json AS attrs_json"
        )
        rows = graph.run_cypher(edge_query, {"service": service}, max_rows=2_000_000)
    else:
        edge_query = (
            "MATCH (a:KBNode)-[r]->(b:KBNode) "
            "RETURN a.id AS src, b.id AS dst, type(r) AS type, r.attributes_json AS attrs_json"
        )
        rows = graph.run_cypher(edge_query, max_rows=2_000_000)

    edges = [
        {"src": row["src"], "dst": row["dst"], "type": row["type"],
         "attributes": json.loads(row.get("attrs_json") or "{}")}
        for row in rows
        # When scoped to one service, drop edges whose *other* endpoint wasn't exported.
        if row["src"] in node_ids and row["dst"] in node_ids
    ]

    payload = {
        "version": _SNAPSHOT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "service": service,
        "nodes": nodes,
        "edges": edges,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str)
    log.info("snapshot_exported", path=str(path), nodes=len(nodes), edges=len(edges))
    return {"nodes": len(nodes), "edges": len(edges)}


def import_snapshot(graph: GraphStorePort, path: Path) -> dict[str, int]:
    """Load a gzip-compressed JSON snapshot (from `export_snapshot`) and upsert
    its nodes/edges into `graph`. Unknown node/edge types are skipped, not fatal,
    so snapshots remain forward-compatible with older/newer schema versions.
    """
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload: dict[str, Any] = json.load(fh)

    nodes: list[GraphNode] = []
    skipped_nodes = 0
    for raw in payload.get("nodes", []):
        try:
            node_type = NodeType(raw["type"])
        except ValueError:
            skipped_nodes += 1
            continue
        nodes.append(GraphNode(id=raw["id"], type=node_type, name=raw.get("name") or raw["id"],
                               service=raw.get("service"), attributes=raw.get("attributes") or {}))

    edges: list[GraphEdge] = []
    skipped_edges = 0
    for raw in payload.get("edges", []):
        try:
            edge_type = EdgeType(raw["type"])
        except ValueError:
            skipped_edges += 1
            continue
        edges.append(GraphEdge(src=raw["src"], dst=raw["dst"], type=edge_type,
                               attributes=raw.get("attributes") or {}))

    graph.add_many(nodes, edges)
    graph.save()
    log.info("snapshot_imported", path=str(path), nodes=len(nodes), edges=len(edges),
             skipped_nodes=skipped_nodes, skipped_edges=skipped_edges)
    return {"nodes": len(nodes), "edges": len(edges),
            "skipped_nodes": skipped_nodes, "skipped_edges": skipped_edges}
