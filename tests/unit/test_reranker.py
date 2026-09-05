"""Unit tests for the pluggable reranker (retrieval/reranker.py).

Only exercises the Noop path + factory fallback logic here — the actual
cross-encoder model download is exercised manually / in an integration
environment once RERANK_ENABLED=true and a model is pulled (see
docs/ENTERPRISE_ARCHITECTURE.md).
"""
from __future__ import annotations

from mcp_kb.models import Chunk, ContentType, RetrievedChunk
from mcp_kb.retrieval.reranker import NoopReranker, get_reranker


def _chunk(cid: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(id=cid, repo="rx-order", rel_path="Foo.java",
                    content_type=ContentType.JAVA, collection="code", text=text),
        score=1.0,
    )


def test_noop_reranker_preserves_order_and_truncates():
    chunks = [_chunk("a", "alpha"), _chunk("b", "beta"), _chunk("c", "gamma")]
    out = NoopReranker().rerank("query", chunks, top_k=2)
    assert [rc.chunk.id for rc in out] == ["a", "b"]


def test_factory_returns_noop_when_disabled(settings, monkeypatch):
    import mcp_kb.retrieval.reranker as reranker_mod

    monkeypatch.setattr(reranker_mod, "_RERANKER", None)
    settings.rerank_enabled = False
    reranker = get_reranker(settings)
    assert isinstance(reranker, NoopReranker)


def test_factory_falls_back_to_noop_on_unknown_provider(settings, monkeypatch):
    import mcp_kb.retrieval.reranker as reranker_mod

    monkeypatch.setattr(reranker_mod, "_RERANKER", None)
    settings.rerank_enabled = True
    settings.rerank_provider = "not-a-real-provider"
    reranker = get_reranker(settings)
    assert isinstance(reranker, NoopReranker)
