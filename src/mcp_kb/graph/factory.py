"""Graph backend factory for the mandatory Neo4j runtime backend."""
from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import GraphStorePort

log = get_logger(__name__)


def get_graph_store(settings: Settings) -> GraphStorePort:
    """Return the Neo4j graph store; fail fast for unsupported backends."""
    backend = (settings.graph_backend or "neo4j").lower()
    if backend != "neo4j":
        raise ValueError(
            f"Unsupported runtime graph backend: {backend}. Neo4j is required."
        )
    from .neo4j_store import Neo4jGraphStore

    log.info("graph_backend_selected", backend="neo4j", uri=settings.neo4j_uri)
    return Neo4jGraphStore(settings)
