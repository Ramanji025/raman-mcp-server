"""Unit test for reciprocal-rank-fusion in the hybrid retriever."""
from __future__ import annotations

from mcp_kb.models import Chunk, ContentType, RetrievedChunk
from mcp_kb.retrieval.hybrid_retriever import HybridRetriever


def _rc(cid: str, score: float) -> RetrievedChunk:
    chunk = Chunk(id=cid, repo="r", rel_path=f"{cid}.java",
                  content_type=ContentType.JAVA, collection="code", text="x")
    return RetrievedChunk(chunk=chunk, score=score)


def test_rrf_prefers_items_ranked_high_in_multiple_lists(settings, monkeypatch):
    # Avoid loading embedder / qdrant during construction.
    monkeypatch.setattr(HybridRetriever, "__init__",
                        lambda self, *a, **k: None)
    r = HybridRetriever.__new__(HybridRetriever)
    r._rrf_k = 60

    list_a = [_rc("a", 0.9), _rc("b", 0.8), _rc("c", 0.7)]
    list_b = [_rc("b", 0.95), _rc("a", 0.6), _rc("d", 0.5)]
    fused = r._reciprocal_rank_fusion([list_a, list_b])

    ids = [rc.chunk.id for rc in fused]
    # 'a' and 'b' appear in both lists near the top -> should lead.
    assert ids[0] in {"a", "b"}
    assert ids[1] in {"a", "b"}
    assert set(ids) == {"a", "b", "c", "d"}
    assert all(rc.source == "fused" for rc in fused)


def test_three_way_rrf_graph_list_boosts_overlap(settings, monkeypatch):
    monkeypatch.setattr(HybridRetriever, "__init__", lambda self, *a, **k: None)
    r = HybridRetriever.__new__(HybridRetriever)
    r._rrf_k = 60
    dense = [_rc("a", 0.9), _rc("b", 0.4), _rc("c", 0.3)]
    sparse = [_rc("c", 0.9), _rc("a", 0.5)]
    graph = [_rc("c", 0.8), _rc("a", 0.7)]
    fused = r._reciprocal_rank_fusion([dense, sparse, graph])
    ids = [rc.chunk.id for rc in fused]
    assert ids[0] in {"a", "c"}
    assert set(ids) == {"a", "b", "c"}
