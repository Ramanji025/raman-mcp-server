"""Hybrid retriever combining Qdrant vector search with graph expansion.

Implements RAG + Knowledge-Graph fusion (goal #9): vector hits seed relevant
graph nodes, graph neighbours contribute structural context, and results are
merged with reciprocal rank fusion, favouring accuracy over latency (goal #13).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import Settings
from ..graph.base import GraphStorePort
from ..graph.factory import get_graph_store
from ..learning.online import apply_retrieval_boosts
from ..logging import get_logger
from ..models import RetrievedChunk
from ..observability.tracing import record_retrieval_quality
from ..vector.embeddings import Embedder, get_embedder
from ..vector.qdrant_store import QdrantStore
from ..vector.sparse import get_sparse_embedder
from .lexical import lexical_rank
from .reranker import Reranker, get_reranker

log = get_logger(__name__)


@dataclass
class HybridResult:
    """Fused result of one hybrid (dense+sparse+graph) retrieval call."""

    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    graph_nodes: list[dict] = field(default_factory=list)
    services: list[str] = field(default_factory=list)


class HybridRetriever:
    """Combines Qdrant dense+sparse search with graph expansion and reranking."""

    def __init__(
        self,
        settings: Settings,
        store: QdrantStore | None = None,
        graph: GraphStorePort | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder or get_embedder(settings)
        self._store = store or QdrantStore(settings, self._embedder.dim)
        self._graph = graph or get_graph_store(settings)
        self._reranker = reranker or get_reranker(settings)
        cfg = settings.static.get("retrieval", {})
        self._vector_top_k = cfg.get("vector_top_k", 20)
        self._graph_hops = cfg.get("graph_expansion_hops", 2)
        self._rerank_top_k = cfg.get("rerank_top_k", 8)
        self._rrf_k = cfg.get("rrf_k", 60)

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        query: str,
        *,
        collections: Sequence[str] = ("semantic", "code", "docs", "architecture",
                                      "defects", "incidents"),
        service: str | None = None,
        kind: str | None = None,
        top_k: int | None = None,
        boosts: dict[str, float] | None = None,
    ) -> HybridResult:
        """Run hybrid dense+sparse retrieval, RRF fusion, graph expansion, and reranking for a query."""
        vector = self._embedder.embed_query(query)
        sparse = None
        if self._settings.sparse_enabled:
            encoder = get_sparse_embedder(self._settings)
            if encoder is not None:
                try:
                    sparse = encoder.embed_query(query)
                except Exception as exc:
                    log.warning("sparse_query_embed_failed", error=str(exc))

        dense_lists: list[list[RetrievedChunk]] = []
        sparse_lists: list[list[RetrievedChunk]] = []
        for collection in collections:
            hits = self._store.search(
                collection, vector, top_k=self._vector_top_k,
                service=service, kind=kind, using="dense",
            )
            if hits:
                dense_lists.append(hits)
            if sparse is not None:
                lex = self._store.search(
                    collection, vector, top_k=self._vector_top_k,
                    service=service, kind=kind, query_sparse=sparse,
                    using="bm25",
                )
                if lex:
                    sparse_lists.append(lex)

        ranked_lists = dense_lists + sparse_lists
        fused = self._reciprocal_rank_fusion(ranked_lists) if ranked_lists else []
        if fused and not sparse_lists:
            fused = self._reciprocal_rank_fusion([fused, lexical_rank(query, fused)])

        graph_preview = self._expand_graph(query, fused[:24] if fused else [])
        graph_list: list[RetrievedChunk] = []
        names = {str(n.get("name", "")).lower() for n in graph_preview if n.get("name")}
        names.discard("")
        if names and fused:
            for rc in fused:
                blob = (rc.chunk.text or "").lower()
                if any(nm in blob for nm in names):
                    graph_list.append(RetrievedChunk(
                        chunk=rc.chunk, score=rc.score, source="graph",
                    ))
            if graph_list:
                fused = self._reciprocal_rank_fusion([fused, graph_list])
        limit = top_k or self._rerank_top_k
        # When enabled, widen the candidate pool beyond `limit` so the
        # cross-encoder has real precision headroom to correct RRF's
        # ordering, rather than just re-sorting an already-truncated list.
        candidate_k = max(limit, self._settings.rerank_candidate_k) \
            if self._settings.rerank_enabled else limit
        top = self._reranker.rerank(query, fused[:candidate_k], limit)
        if boosts:
            top = apply_retrieval_boosts(top, boosts)

        graph_nodes = self._expand_graph(query, top)
        services = sorted({
            c.chunk.metadata.get("service") for c in top
            if c.chunk.metadata.get("service")
        } | {n.get("service") for n in graph_nodes if n.get("service")})

        record_retrieval_quality(query, chunk_count=len(top),
                                 top_score=top[0].score if top else 0.0)
        return HybridResult(query=query, chunks=top, graph_nodes=graph_nodes,
                            services=services)

    # ------------------------------------------------------------------ #
    def _reciprocal_rank_fusion(
        self, ranked_lists: list[list[RetrievedChunk]]
    ) -> list[RetrievedChunk]:
        scores: dict[str, float] = defaultdict(float)
        best: dict[str, RetrievedChunk] = {}
        for ranked in ranked_lists:
            for rank, item in enumerate(ranked):
                key = item.chunk.id
                scores[key] += 1.0 / (self._rrf_k + rank + 1)
                if key not in best or item.score > best[key].score:
                    best[key] = item
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[RetrievedChunk] = []
        for key, fused_score in ordered:
            item = best[key]
            if item.chunk.collection == "semantic":
                fused_score += 0.25
            out.append(RetrievedChunk(chunk=item.chunk, score=fused_score, source="fused"))
        return out

    def _expand_graph(self, query: str, chunks: list[RetrievedChunk]) -> list[dict]:
        """Seed graph nodes from retrieved chunks + direct name matches."""
        seeds: dict[str, dict] = {}

        # 1) Nodes whose name appears in the query text.
        for node in self._graph.find_nodes(query, limit=8):
            seeds[node["id"]] = node

        # 2) Nodes referenced by retrieved code chunks (class / endpoint).
        for rc in chunks:
            meta = rc.chunk.metadata
            for key in ("class", "handler", "fqcn"):
                name = meta.get(key)
                if not name:
                    continue
                for node in self._graph.find_nodes(str(name).split(".")[-1], limit=3):
                    seeds[node["id"]] = node

        # 3) One-hop neighbours for structural context; deeper for cross-service intents.
        expanded = dict(seeds)
        hops_budget = getattr(self, "_graph_hops", 2)
        for node_id in list(seeds):
            for hop in self._graph.neighbors(node_id, direction="out")[:6]:
                n = hop.get("node")
                if n:
                    expanded[n["id"]] = {**n, "via": hop["edge_type"]}
        # second hop when budget allows
        if hops_budget >= 2:
            for node_id in list(dict(seeds)):
                for hop in self._graph.neighbors(node_id, direction="in")[:4]:
                    n = hop.get("node")
                    if n:
                        expanded[n["id"]] = {**n, "via": hop["edge_type"]}
        return list(expanded.values())[:25]

    @property
    def graph(self) -> GraphStorePort:
        """The underlying graph store."""
        return self._graph

    @property
    def store(self) -> QdrantStore:
        """The underlying Qdrant vector store."""
        return self._store

    @property
    def embedder(self) -> Embedder:
        """The underlying embedding backend."""
        return self._embedder
