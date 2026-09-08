"""Graph backend factory: Neo4j (default, scale-out) or NetworkX (Phase 7:
officially-supported zero-infrastructure local mode — JSON-file persisted,
no database to install/run, matching codebase-memory-mcp's "no
infrastructure required" positioning for smaller/demo/evaluation use).
"""
from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import GraphStorePort

log = get_logger(__name__)


def get_graph_store(settings: Settings) -> GraphStorePort:
    """Return the configured graph backend (GRAPH_BACKEND=neo4j|networkx)."""
    backend = (settings.graph_backend or "neo4j").lower()
    if backend == "networkx":
        from .knowledge_graph import KnowledgeGraph

        log.info("graph_backend_selected", backend="networkx",
                hint="zero-infrastructure local mode — query_graph/ad-hoc Cypher "
                     "tools are unavailable on this backend")
        return KnowledgeGraph(settings)
    if backend != "neo4j":
        raise ValueError(
            f"Unsupported GRAPH_BACKEND: {backend!r}. Use 'neo4j' (default, scale-out) "
            f"or 'networkx' (zero-infrastructure local mode)."
        )
    from .neo4j_store import Neo4jGraphStore

    log.info("graph_backend_selected", backend="neo4j", uri=settings.neo4j_uri)
    return Neo4jGraphStore(settings)

