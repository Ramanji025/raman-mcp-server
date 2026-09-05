"""Batched embedding + upsert orchestration into Qdrant."""
from __future__ import annotations

import time
from collections.abc import Sequence

from ..config import Settings
from ..logging import get_logger
from ..models import Chunk
from ..observability.tracing import record_embedding_batch
from ..security.sensitive_scanner import scan_and_redact_chunk_text
from .embeddings import Embedder, get_embedder
from .qdrant_store import QdrantStore
from .sparse import get_sparse_embedder

log = get_logger(__name__)


class VectorIndexer:
    """Turns parsed chunks into vectors and writes them to Qdrant."""

    def __init__(self, settings: Settings, store: QdrantStore | None = None,
                 embedder: Embedder | None = None) -> None:
        self._settings = settings
        self._embedder = embedder or get_embedder(settings)
        self._store = store or QdrantStore(settings, self._embedder.dim)
        self._store.ensure_collections()

    @property
    def store(self) -> QdrantStore:
        """The underlying Qdrant store."""
        return self._store

    def index(self, chunks: Sequence[Chunk]) -> int:
        """Redact secrets, embed, and upsert a batch of chunks into Qdrant; return count written."""
        if not chunks:
            return 0
        # Redact high-confidence secrets (API keys, private keys, hardcoded
        # passwords, ...) before they ever reach the embedder or Qdrant.
        # PII patterns (email/SSN/credit-card) are intentionally NOT
        # auto-redacted here — see security/sensitive_scanner.py docstring.
        safe_chunks = []
        for c in chunks:
            redacted_text, findings = scan_and_redact_chunk_text(c.text, self._settings)
            if findings:
                safe_chunks.append(c.model_copy(update={"text": redacted_text}))
            else:
                safe_chunks.append(c)

        batch = self._settings.embedding_batch_size
        written = 0
        for start in range(0, len(safe_chunks), batch):
            window = list(safe_chunks[start : start + batch])
            t0 = time.perf_counter()
            vectors = self._embedder.embed([c.text for c in window])
            record_embedding_batch(batch_size=len(window),
                                   elapsed_ms=(time.perf_counter() - t0) * 1000)
            sparse = None
            if self._settings.sparse_enabled:
                encoder = get_sparse_embedder(self._settings)
                if encoder is not None:
                    try:
                        sparse = encoder.embed([c.text for c in window])
                    except Exception as exc:
                        log.warning("sparse_embed_failed", error=str(exc))
            written += self._store.upsert(window, vectors, sparse_vectors=sparse)
        log.info("vectors_written", count=written)
        return written

    def remove_files(self, repo: str, rel_paths: Sequence[str]) -> None:
        """Delete previously-indexed vectors for the given repo files."""
        self._store.delete_by_paths(repo, rel_paths)
