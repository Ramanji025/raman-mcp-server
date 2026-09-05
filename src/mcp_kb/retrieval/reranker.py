"""Enterprise RAG precision stage: cross-encoder re-ranking.

Reciprocal-rank fusion (in ``hybrid_retriever.py``) is a fast, robust way to
merge multiple per-type vector searches, but bi-encoder similarity alone
still lets topically-adjacent-but-wrong chunks outrank the true best answer.
A cross-encoder reranker scores ``(query, chunk)`` pairs jointly and
re-orders the fused candidates before the final top-k cut.

This is disabled by default (``RERANK_ENABLED=false``) so behaviour and
latency are unchanged until a model is explicitly chosen — flipping it on
is a config change, not a code change:

    RERANK_ENABLED=true
    RERANK_PROVIDER=cross-encoder
    RERANK_MODEL=BAAI/bge-reranker-v2-m3        # or a Jina reranker id

``sentence-transformers`` (already a core dependency) ships a generic
``CrossEncoder`` wrapper that loads any of these Hugging Face rerankers.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..config import Settings
from ..logging import get_logger
from ..models import RetrievedChunk

log = get_logger(__name__)


class Reranker(Protocol):
    """Protocol for reordering retrieved chunks by relevance to the query."""

    def rerank(self, query: str, chunks: Sequence[RetrievedChunk],
               top_k: int) -> list[RetrievedChunk]:
        """Reorder `chunks` by relevance to `query` and return the top `top_k`."""
        ...


class NoopReranker:
    """Identity reranker — preserves the RRF-fused ordering unchanged."""

    def rerank(self, query: str, chunks: Sequence[RetrievedChunk],
               top_k: int) -> list[RetrievedChunk]:
        """Return the first `top_k` chunks unchanged (no reranking)."""
        return list(chunks[:top_k])


class CrossEncoderReranker:
    """Local cross-encoder reranker via ``sentence-transformers``.

    Works with any Hugging Face cross-encoder id, including
    ``BAAI/bge-reranker-v2-m3`` and Jina's ``jinaai/jina-reranker-v2-base-multilingual``
    once downloaded — set ``RERANK_MODEL`` accordingly.
    """

    def __init__(self, settings: Settings) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(settings.rerank_model)
        log.info("reranker_loaded", model=settings.rerank_model)

    def rerank(self, query: str, chunks: Sequence[RetrievedChunk],
               top_k: int) -> list[RetrievedChunk]:
        """Score each chunk against the query with the cross-encoder and return the top `top_k`."""
        if not chunks:
            return []
        pairs = [(query, rc.chunk.text[:2000]) for rc in chunks]
        scores = self._model.predict(pairs)
        rescored = [
            RetrievedChunk(chunk=rc.chunk, score=float(score), source="reranked")
            for rc, score in zip(chunks, scores)
        ]
        rescored.sort(key=lambda rc: rc.score, reverse=True)
        return rescored[:top_k]


_RERANKER: Reranker | None = None


def get_reranker(settings: Settings) -> Reranker:
    """Process-wide reranker singleton, honouring ``RERANK_ENABLED``."""
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    if not settings.rerank_enabled:
        _RERANKER = NoopReranker()
        return _RERANKER
    provider = settings.rerank_provider.lower()
    try:
        if provider == "cross-encoder":
            _RERANKER = CrossEncoderReranker(settings)
        else:
            log.warning("unknown_rerank_provider_falling_back_to_noop", provider=provider)
            _RERANKER = NoopReranker()
    except Exception as exc:  # pragma: no cover - missing model/deps at runtime
        log.warning("reranker_init_failed_falling_back_to_noop", error=str(exc))
        _RERANKER = NoopReranker()
    return _RERANKER
