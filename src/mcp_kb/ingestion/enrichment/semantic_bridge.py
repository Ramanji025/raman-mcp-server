"""Semantic (vocabulary-mismatch) bridging edges between methods.

Mirrors codebase-memory-mcp's SEMANTICALLY_RELATED edges: cross-file links
between symbols whose *names* don't share tokens but whose *behavior*
(embedded body text) is similar — e.g. "create user" <-> `UserService.
provision()`. Complements SIMILAR_TO (near-duplicate code) with a
same-service, embedding-cosine bridge that surface tools/impact-analysis
can traverse as graph edges instead of only as ad-hoc vector search hits.

Best-effort / bounded: pairwise cosine within one service, skipped above a
size cap to keep ingestion time bounded (see SEMANTIC_BRIDGE_MAX_METHODS).
"""
from __future__ import annotations

import math

from ...config import Settings
from ...graph.base import GraphStorePort
from ...logging import get_logger
from ...models import EdgeType, GraphEdge, NodeType
from ...vector.embeddings import get_embedder

log = get_logger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticBridgeEnrichment:
    """Computes SEMANTICALLY_RELATED edges across Method nodes within one service."""

    def __init__(self, settings: Settings, graph: GraphStorePort) -> None:
        self._settings = settings
        self.graph = graph
        self._threshold = settings.semantic_bridge_min_cosine
        self._max_methods = settings.semantic_bridge_max_methods

    def run(self, service: str) -> int:
        """Compute and write SEMANTICALLY_RELATED edges for one service; return edge count."""
        methods = self.graph.nodes_by_type(NodeType.METHOD, service=service)
        if len(methods) < 2:
            return 0
        if len(methods) > self._max_methods:
            log.info("semantic_bridge_skipped_too_large", service=service,
                     methods=len(methods), cap=self._max_methods)
            return 0

        embedder = get_embedder(self._settings)
        texts = []
        for node in methods:
            attrs = node.get("attributes") or {}
            summary = attrs.get("llm_summary") or ""
            name = node.get("name") or ""
            body = (attrs.get("body") or "")[:600]
            texts.append(f"{name} {summary} {body}".strip())
        vectors = embedder.embed(texts)

        edges: list[GraphEdge] = []
        for i in range(len(methods)):
            name_i = (methods[i].get("name") or "").lower()
            for j in range(i + 1, len(methods)):
                name_j = (methods[j].get("name") or "").lower()
                # Skip pairs that already share an obvious name token — that's
                # a lexical match, not the vocabulary-mismatch bridge we want.
                if name_i and name_j and (name_i in name_j or name_j in name_i):
                    continue
                score = _cosine(vectors[i], vectors[j])
                if score >= self._threshold:
                    edges.append(GraphEdge(
                        src=methods[i]["id"], dst=methods[j]["id"],
                        type=EdgeType.SEMANTICALLY_RELATED,
                        attributes={"cosine": round(score, 3)},
                    ))
        if edges:
            self.graph.add_many([], edges)
            self.graph.save()
        log.info("semantic_bridge_done", service=service, methods=len(methods),
                 edges_created=len(edges))
        return len(edges)
