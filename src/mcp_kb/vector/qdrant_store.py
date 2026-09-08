"""Qdrant-backed vector store with one collection per content type."""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from ..config import Settings
from ..logging import get_logger
from ..models import Chunk, RetrievedChunk

log = get_logger(__name__)


def _point_id(chunk_id: str) -> str:
    """Qdrant point ids must be UUIDs or ints; derive a stable UUID5."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


class QdrantStore:
    """Thin, accuracy-tuned wrapper over the Qdrant client."""

    def __init__(self, settings: Settings, dim: int) -> None:
        self._settings = settings
        self._dim = dim
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            # Follow-on gap 1 perf: gRPC (binary, HTTP/2) materially outperforms
            # REST for bulk upsert/search throughput. Off by default for
            # zero-config compatibility (REST-only Qdrant deployments still
            # work); enable via QDRANT_PREFER_GRPC=true once the gRPC port is
            # confirmed reachable.
            prefer_grpc=settings.qdrant_prefer_grpc,
            grpc_port=settings.qdrant_grpc_port,
            check_compatibility=False,
        )
        static = settings.static.get("vector", {})
        self._distance = getattr(qm.Distance, static.get("distance", "Cosine").upper(),
                                 qm.Distance.COSINE)
        self._hnsw = static.get("hnsw", {})
        configured = static.get("collections", ["code", "docs", "architecture", "defects", "incidents"])
        required = ["semantic", "code", "docs", "architecture", "defects", "incidents"]
        # Keep configured ordering but always include required collections used by runtime chunks.
        merged: list[str] = []
        for c in list(configured) + required:
            if c not in merged:
                merged.append(c)
        self._collections = merged
        self._layouts: dict[str, str] = {}
        # Perf: cache which collections are confirmed to exist so hot-path
        # upsert()/search() calls skip a network round-trip existence check
        # on every single call — collections don't disappear mid-process in
        # our usage pattern (only reset_collections() removes them, which
        # also invalidates this cache).
        self._known_existing: set[str] = set()

    def collection_layout(self, name: str) -> str:
        """Return ``hybrid`` (named dense+bm25), ``unnamed`` (legacy dense), or ``missing``."""
        if name in self._layouts:
            return self._layouts[name]
        if not self.client.collection_exists(name):
            return "missing"
        try:
            info = self.client.get_collection(name)
            params = info.config.params
            vectors = params.vectors
            sparse = getattr(params, "sparse_vectors", None) or {}
            named = isinstance(vectors, dict)
            if named and "dense" in vectors and sparse:
                layout = "hybrid"
            else:
                layout = "unnamed"
        except Exception:
            layout = "unnamed"
        self._layouts[name] = layout
        return layout

    # ---- lifecycle ---- #
    def _create_collection(self, name: str) -> None:
        log.info("create_collection", name=name, dim=self._dim,
                 sparse=self._settings.sparse_enabled)
        hnsw = qm.HnswConfigDiff(
            m=self._hnsw.get("m", 32),
            ef_construct=self._hnsw.get("ef_construct", 256),
        )
        on_disk = self._settings.qdrant_on_disk_vectors
        if self._settings.sparse_enabled:
            self.client.create_collection(
                collection_name=name,
                vectors_config={
                    "dense": qm.VectorParams(size=self._dim, distance=self._distance,
                                             on_disk=on_disk),
                },
                sparse_vectors_config={
                    "bm25": qm.SparseVectorParams(),
                },
                hnsw_config=hnsw,
            )
            self._layouts[name] = "hybrid"
        else:
            self.client.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=self._dim, distance=self._distance,
                                              on_disk=on_disk),
                hnsw_config=hnsw,
            )
            self._layouts[name] = "unnamed"
        self._known_existing.add(name)
        # Payload indexes accelerate filtered retrieval by service/type.
        for field, schema in (
            ("service", qm.PayloadSchemaType.KEYWORD),
            ("content_type", qm.PayloadSchemaType.KEYWORD),
            ("kind", qm.PayloadSchemaType.KEYWORD),
            ("repo", qm.PayloadSchemaType.KEYWORD),
        ):
            try:
                self.client.create_payload_index(name, field, schema)
            except Exception:  # index may already exist
                pass

    def _existing_dim(self, name: str) -> int | None:
        """Configured dense dimension of an existing collection, or None if unknown."""
        try:
            vectors = self.client.get_collection(name).config.params.vectors
        except Exception:
            return None
        if isinstance(vectors, dict):
            dense = vectors.get("dense") or next(iter(vectors.values()), None)
            return getattr(dense, "size", None)
        return getattr(vectors, "size", None)

    def _ensure_collection_exists(self, name: str) -> None:
        if name in self._known_existing:
            return
        if self.client.collection_exists(name):
            self._known_existing.add(name)
            actual = self._existing_dim(name)
            if actual is not None and actual != self._dim:
                raise RuntimeError(
                    f"Qdrant collection {name!r} was built for {actual}-dim vectors but "
                    f"EMBEDDING_DIM is {self._dim}. The embedding model changed; recreate "
                    f"the collections and re-ingest:\n"
                    f"    python -m mcp_kb.vector.qdrant_store --reset"
                )
            return
        self._create_collection(name)

    def reset_collections(self) -> list[str]:
        """Drop and recreate every collection. Destroys all indexed vectors."""
        dropped: list[str] = []
        for content_type in self._collections:
            name = self._settings.collection_name(content_type)
            if self.client.collection_exists(name):
                self.client.delete_collection(name)
                dropped.append(name)
            self._layouts.pop(name, None)
            self._known_existing.discard(name)
            self._create_collection(name)
        return dropped

    def ensure_collections(self) -> None:
        """Create every configured collection that doesn't already exist."""
        for content_type in self._collections:
            name = self._settings.collection_name(content_type)
            self._ensure_collection_exists(name)

    # ---- writes ---- #
    def upsert(
        self,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        sparse_vectors: Sequence[Any] | None = None,
        *,
        wait: bool | None = None,
    ) -> int:
        """Upsert chunks with their dense (and optional sparse) vectors; return
        count written. Uses qdrant-client's `upload_points` (batched +
        parallelized, size/parallelism from QDRANT_UPSERT_BATCH_SIZE/
        QDRANT_UPSERT_PARALLEL) instead of one blocking call per collection —
        the standard high-throughput bulk-ingestion primitive.

        `wait`: override QDRANT_UPSERT_WAIT for this call. Leave unset
        (defaults to the fast, non-blocking async-ack path) for bulk
        ingestion; pass `wait=True` only when a caller needs the write
        durably indexed before its next read (e.g. a single-chunk
        interactive update immediately followed by a search).
        """
        by_collection: dict[str, list[qm.PointStruct]] = {}
        for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
            name = self._settings.collection_name(chunk.collection)
            payload = {
                "chunk_id": chunk.id,
                "repo": chunk.repo,
                "rel_path": chunk.rel_path,
                "content_type": chunk.content_type.value,
                "service": chunk.metadata.get("service", chunk.repo),
                "kind": chunk.metadata.get("kind", ""),
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text,
                **{f"meta_{k}": v for k, v in chunk.metadata.items()},
            }
            layout = self.collection_layout(name)
            if layout == "missing":
                self._ensure_collection_exists(name)
                layout = self.collection_layout(name)
            sparse = None
            if sparse_vectors is not None and i < len(sparse_vectors):
                sparse = sparse_vectors[i]
            if layout == "hybrid":
                named: dict[str, Any] = {"dense": list(vector)}
                if sparse is not None:
                    named["bm25"] = sparse
                vec: Any = named
            else:
                vec = list(vector)
            by_collection.setdefault(name, []).append(
                qm.PointStruct(id=_point_id(chunk.id), vector=vec, payload=payload)
            )
        total = 0
        effective_wait = self._settings.qdrant_upsert_wait if wait is None else wait
        for name, points in by_collection.items():
            # Defensive auto-heal: if runtime emits a collection not present at startup,
            # create it on-demand instead of failing the whole ingestion run.
            self._ensure_collection_exists(name)
            self.client.upload_points(
                collection_name=name,
                points=points,
                batch_size=self._settings.qdrant_upsert_batch_size,
                parallel=self._settings.qdrant_upsert_parallel,
                wait=effective_wait,
            )
            total += len(points)
        return total

    def delete_by_ids(self, chunk_ids: Iterable[str], *, wait: bool = True) -> None:
        """Delete specific chunks by id across all collections — O(1) point-id
        lookup rather than a filtered scan, for surgical single-file/single-
        chunk incremental updates (much cheaper than delete_by_paths when the
        exact set of stale chunk ids is already known)."""
        ids = [_point_id(cid) for cid in chunk_ids]
        if not ids:
            return
        for content_type in self._collections:
            name = self._settings.collection_name(content_type)
            if name in self._known_existing or self.client.collection_exists(name):
                self._known_existing.add(name)
                self.client.delete(name, points_selector=qm.PointIdsList(points=ids), wait=wait)

    def delete_by_paths(self, repo: str, rel_paths: Iterable[str]) -> None:
        """Delete indexed vectors for the given repo files across all collections."""
        paths = list(rel_paths)
        if not paths:
            return
        flt = qm.Filter(must=[
            qm.FieldCondition(key="repo", match=qm.MatchValue(value=repo)),
            qm.FieldCondition(key="rel_path", match=qm.MatchAny(any=paths)),
        ])
        for content_type in self._collections:
            name = self._settings.collection_name(content_type)
            if self.client.collection_exists(name):
                self.client.delete(name, points_selector=qm.FilterSelector(filter=flt),
                                   wait=True)

    # ---- search ---- #
    def _filter(self, service: str | None, kind: str | None) -> qm.Filter | None:
        conditions: list[qm.FieldCondition] = []
        if service:
            conditions.append(
                qm.FieldCondition(key="service", match=qm.MatchValue(value=service)))
        if kind:
            conditions.append(
                qm.FieldCondition(key="kind", match=qm.MatchValue(value=kind)))
        return qm.Filter(must=conditions) if conditions else None

    def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        top_k: int = 20,
        service: str | None = None,
        kind: str | None = None,
        query_sparse: Any | None = None,
        using: str | None = None,
    ) -> list[RetrievedChunk]:
        """Search one collection (dense, sparse, or fused hybrid) for the top `top_k` chunks."""
        name = self._settings.collection_name(collection)
        if name not in self._known_existing:
            if not self.client.collection_exists(name):
                return []
            self._known_existing.add(name)
        flt = self._filter(service, kind)
        ef = self._hnsw.get("ef_search", 64)
        layout = self.collection_layout(name)
        params = qm.SearchParams(hnsw_ef=ef, exact=False)

        if using == "bm25" and layout != "hybrid":
            return []
        if layout != "hybrid" and using == "dense":
            using = None

        if layout == "hybrid" and query_sparse is not None and using is None:
            try:
                result = self.client.query_points(
                    collection_name=name,
                    prefetch=[
                        qm.Prefetch(
                            query=list(query_vector), using="dense",
                            limit=top_k, filter=flt,
                        ),
                        qm.Prefetch(
                            query=query_sparse, using="bm25",
                            limit=top_k, filter=flt,
                        ),
                    ],
                    query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                    limit=top_k,
                    with_payload=True,
                )
                hits = [self._to_retrieved(h, source="hybrid") for h in result.points]
                if hits:
                    return hits
            except Exception as exc:
                log.warning("hybrid_search_failed", collection=name, error=str(exc))

        query: Any = list(query_vector)
        using_name = using
        if layout == "hybrid":
            using_name = using_name or "dense"
            if using_name == "bm25" and query_sparse is not None:
                query = query_sparse
        try:
            kwargs: dict[str, Any] = {
                "collection_name": name,
                "query": query,
                "limit": top_k,
                "query_filter": flt,
                "with_payload": True,
                "search_params": params,
            }
            if using_name:
                kwargs["using"] = using_name
            result = self.client.query_points(**kwargs)
        except TypeError:
            result = self.client.query_points(
                collection_name=name,
                query=list(query_vector),
                limit=top_k,
                query_filter=flt,
                with_payload=True,
                search_params=params,
            )
        source = "sparse" if using_name == "bm25" else "vector"
        return [self._to_retrieved(h, source=source) for h in result.points]

    def _to_retrieved(self, hit: Any, source: str = "vector") -> RetrievedChunk:
        p = hit.payload or {}
        meta = {k[5:]: v for k, v in p.items() if k.startswith("meta_")}
        chunk = Chunk(
            id=p.get("chunk_id", str(hit.id)),
            repo=p.get("repo", ""),
            rel_path=p.get("rel_path", ""),
            content_type=p.get("content_type", "docs"),
            collection=p.get("content_type", "docs"),
            text=p.get("text", ""),
            start_line=p.get("start_line"),
            end_line=p.get("end_line"),
            metadata={**meta, "service": p.get("service"), "kind": p.get("kind")},
        )
        score = float(getattr(hit, "score", 0.0))
        return RetrievedChunk(chunk=chunk, score=score, source=source)

    def count(self, collection: str) -> int:
        """Return the exact point count for one collection (0 if it doesn't exist)."""
        name = self._settings.collection_name(collection)
        if not self.client.collection_exists(name):
            return 0
        return self.client.count(name, exact=True).count


def _cli() -> None:
    import argparse

    from ..config import get_settings

    parser = argparse.ArgumentParser(description="Qdrant collection maintenance")
    parser.add_argument(
        "--reset", action="store_true",
        help="drop and recreate all collections (destroys vectors; re-ingest afterwards)",
    )
    args = parser.parse_args()
    if not args.reset:
        parser.error("nothing to do; pass --reset")

    settings = get_settings()
    store = QdrantStore(settings, dim=settings.embedding_dim)
    dropped = store.reset_collections()
    print(f"Dropped {len(dropped)} collection(s): {', '.join(dropped) or '(none)'}")
    print(f"Recreated all collections at dim={settings.embedding_dim}.")
    print("Now re-ingest:  python -m mcp_kb.ingestion.full_rewrite")


if __name__ == "__main__":
    _cli()
