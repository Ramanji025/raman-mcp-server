"""Pluggable embedding backends (local-first, optional OpenAI)."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


class Embedder(Protocol):
    """Protocol for a pluggable text-embedding backend."""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts into dense vectors."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string into a dense vector."""
        ...


class FastEmbedEmbedder:
    """Local ONNX embeddings via ``fastembed`` (default, no network)."""

    def __init__(self, settings: Settings) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=settings.embedding_model)
        self.dim = settings.embedding_dim
        self._batch = settings.embedding_batch_size

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts locally via fastembed (ONNX)."""
        return [v.tolist() for v in self._model.embed(list(texts), batch_size=self._batch)]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string via fastembed's query-optimized path."""
        return next(iter(self._model.query_embed([text]))).tolist()


class SentenceTransformerEmbedder:
    """Local embeddings via ``sentence-transformers``."""

    def __init__(self, settings: Settings) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(settings.embedding_model)
        self.dim = settings.embedding_dim
        self._batch = settings.embedding_batch_size

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts locally via sentence-transformers."""
        vecs = self._model.encode(
            list(texts), batch_size=self._batch, normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string via sentence-transformers."""
        return self.embed([text])[0]


class OpenAIEmbedder:
    """Remote embeddings via the OpenAI API (opt-in)."""

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=settings.openai_api_key,
                              base_url=settings.openai_base_url or None)
        self._model = settings.embedding_model
        self.dim = settings.embedding_dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts via the OpenAI embeddings API."""
        resp = self._client.embeddings.create(model=self._model, input=list(texts))
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string via the OpenAI embeddings API."""
        return self.embed([text])[0]


_EMBEDDER: Embedder | None = None


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Return the process-wide embedder singleton (settings is unhashable)."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    from ..config import get_settings

    settings = settings or get_settings()
    provider = settings.embedding_provider.lower()
    log.info("embedder_init", provider=provider, model=settings.embedding_model)
    if provider == "sentence-transformers":
        _EMBEDDER = SentenceTransformerEmbedder(settings)
    elif provider == "openai":
        _EMBEDDER = OpenAIEmbedder(settings)
    else:
        _EMBEDDER = FastEmbedEmbedder(settings)
    return _EMBEDDER
