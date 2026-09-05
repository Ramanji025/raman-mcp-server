"""Optional FastEmbed BM25 sparse encoder for Qdrant hybrid search."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

_SPARSE = None
_SPARSE_FAILED = False


def _to_qdrant_sparse(emb: Any):
    from qdrant_client.http import models as qm

    indices = emb.indices
    values = emb.values
    if hasattr(indices, "tolist"):
        indices = indices.tolist()
    if hasattr(values, "tolist"):
        values = values.tolist()
    return qm.SparseVector(
        indices=[int(i) for i in indices],
        values=[float(v) for v in values],
    )


class SparseBm25Embedder:
    """Local BM25 sparse vectors via ``fastembed.SparseTextEmbedding``."""

    def __init__(self, settings: Settings) -> None:
        from fastembed import SparseTextEmbedding

        model = getattr(settings, "sparse_model", None) or "Qdrant/bm25"
        self._model = SparseTextEmbedding(model_name=model)

    def embed(self, texts: Sequence[str]) -> list[Any]:
        """Embed a batch of texts into Qdrant-compatible sparse (BM25) vectors."""
        return [_to_qdrant_sparse(e) for e in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> Any:
        """Embed a single query string into a Qdrant-compatible sparse (BM25) vector."""
        gen = getattr(self._model, "query_embed", None)
        if gen is not None:
            return _to_qdrant_sparse(next(iter(gen([text]))))
        return self.embed([text])[0]


def get_sparse_embedder(settings: Settings | None = None) -> SparseBm25Embedder | None:
    """Process-wide sparse embedder; None if disabled or the model fails to load."""
    global _SPARSE, _SPARSE_FAILED
    from ..config import get_settings

    settings = settings or get_settings()
    if not getattr(settings, "sparse_enabled", True):
        return None
    if _SPARSE_FAILED:
        return None
    if _SPARSE is not None:
        return _SPARSE
    try:
        _SPARSE = SparseBm25Embedder(settings)
        log.info("sparse_embedder_init", model=getattr(settings, "sparse_model", "Qdrant/bm25"))
        return _SPARSE
    except Exception as exc:
        _SPARSE_FAILED = True
        log.warning("sparse_embedder_init_failed", error=str(exc))
        return None
