"""Write DependencyArtifact objects to Neo4j as DEP_ARTIFACT and DEP_FEATURE
nodes, connected to each other and to Service nodes.

Graph topology written
----------------------
(:Service)-[:USES_DEPENDENCY]->(:DEP_ARTIFACT)
(:DEP_ARTIFACT)-[:PROVIDES_FEATURE]->(:DEP_FEATURE)
(:DEP_ARTIFACT)-[:TRANSITIVE_DEP]->(:DEP_ARTIFACT)
"""
from __future__ import annotations

import hashlib

from ...graph.factory import get_graph_store
from ...logging import get_logger
from ...models import (
    DependencyArtifact,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)

log = get_logger(__name__)

_WRITE_BATCH = 300


def _feat_id(name: str) -> str:
    return "feat:" + hashlib.sha256(name.lower().encode()).hexdigest()[:16]


def _svc_id(service: str) -> str:
    # Must match the id produced by PomParser._svc_id for cross-linking to work
    from ..parsers.base import make_node_id
    return make_node_id("service", service, service)


class DependencyGraphWriter:
    """Writes dependency intelligence results to Neo4j."""

    def __init__(self) -> None:
        from ...config import get_settings
        self._store = get_graph_store(get_settings())

    # ------------------------------------------------------------------ #

    def write(
        self,
        artifacts: list[DependencyArtifact],
        transitive_edges: list[tuple[str, str]],
    ) -> tuple[int, int]:
        """Persist all nodes and edges.  Returns (nodes_written, edges_written)."""
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # Canonical artifact nodes
        feature_ids_seen: set[str] = set()
        for da in artifacts:
            nodes.append(_artifact_node(da))

            # Service → Artifact edges (one per declaring service)
            for svc in da.services:
                edges.append(GraphEdge(
                    src=_svc_id(svc),
                    dst=da.id,
                    type=EdgeType.USES_DEPENDENCY,
                    attributes={"scope": da.scope, "ecosystem": da.ecosystem},
                ))

            # Feature nodes + Artifact → Feature edges
            for feat in da.features:
                fid = _feat_id(feat)
                if fid not in feature_ids_seen:
                    nodes.append(_feature_node(fid, feat))
                    feature_ids_seen.add(fid)
                edges.append(GraphEdge(
                    src=da.id,
                    dst=fid,
                    type=EdgeType.PROVIDES_FEATURE,
                    attributes={"ecosystem": da.ecosystem},
                ))

        # Transitive dependency edges
        for src_id, dst_id in transitive_edges:
            edges.append(GraphEdge(
                src=src_id,
                dst=dst_id,
                type=EdgeType.TRANSITIVE_DEP,
                attributes={},
            ))

        # Batch write
        for i in range(0, len(nodes), _WRITE_BATCH):
            self._store.add_many(nodes[i:i + _WRITE_BATCH], [])
        for i in range(0, len(edges), _WRITE_BATCH):
            self._store.add_many([], edges[i:i + _WRITE_BATCH])

        log.info("dep_intel_graph_written",
                 artifacts=len(artifacts),
                 features=len(feature_ids_seen),
                 edges=len(edges),
                 transitive=len(transitive_edges))
        return len(nodes), len(edges)


# --------------------------------------------------------------------------- #

def _artifact_node(da: DependencyArtifact) -> GraphNode:
    return GraphNode(
        id=da.id,
        type=NodeType.DEP_ARTIFACT,
        name=f"{da.group}:{da.artifact}" if da.group else da.artifact,
        service=None,
        attributes={
            "ecosystem": da.ecosystem,
            "group": da.group,
            "artifact": da.artifact,
            "version": da.version,
            "scope": da.scope,
            "optional": da.optional,
            "is_spring_starter": da.is_spring_starter,
            "is_internal": da.is_internal,
            "features": da.features,
            "concept_names": da.concept_names,
            "services": da.services,
        },
    )


def _feature_node(fid: str, name: str) -> GraphNode:
    return GraphNode(
        id=fid,
        type=NodeType.DEP_FEATURE,
        name=name,
        service=None,
        attributes={"feature": name},
    )
