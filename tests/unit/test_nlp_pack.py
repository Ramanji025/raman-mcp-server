"""NLP understand + extractive evidence pack (no Qdrant/LLM)."""
from __future__ import annotations

from mcp_kb.models import Chunk, ContentType, RetrievedChunk
from mcp_kb.nlp.evidence_pack import build_evidence_pack
from mcp_kb.nlp.query_understand import (
    expand_query,
    link_entities,
    parse_acceptance_criteria,
    tokenize,
    understand,
)
from mcp_kb.retrieval.hybrid_retriever import HybridResult
from mcp_kb.retrieval.lexical import lexical_rank


def test_tokenize_keeps_annotations_and_paths():
    tokens = tokenize("What is @Transactional on /api/orders?")
    assert "@Transactional" in tokens or any("Transactional" in t for t in tokens)
    assert any("api" in t.lower() for t in tokens)


def test_parse_given_when_then():
    clauses = parse_acceptance_criteria(
        "Given a paid order When the user refunds Then the status is REFUNDED"
    )
    kinds = [k for k, _ in clauses]
    assert "given" in kinds
    assert "when" in kinds
    assert "then" in kinds


def test_link_entities_substring_and_fuzzy():
    hits = link_entities(
        "tell me about rx-order-service checkout",
        ["rx-order-service", "inventory-service", "PricingController"],
    )
    names = [n for n, _ in hits]
    assert "rx-order-service" in names


def test_expand_query_adds_tech_synonyms():
    expanded = expand_query("How does @Transactional work?", [])
    assert "transaction" in expanded.lower()


def test_understand_classifies_and_expands():
    u = understand(
        "How does eligibility checking work?",
        services=["rx-order-service"],
    )
    assert u.tokens
    assert u.route is not None
    assert u.route.handler == "search_code"
    assert u.expanded


def test_evidence_pack_extracts_sentences_and_confidence():
    chunk = Chunk(
        id="c1", repo="rx-order", rel_path="OrderService.java",
        content_type=ContentType.JAVA, collection="code",
        text="Eligibility is checked in validateCustomer. The method calls PricingClient.",
        start_line=10,
    )
    retrieval = HybridResult(
        query="eligibility",
        chunks=[RetrievedChunk(chunk=chunk, score=0.8, source="vector")],
        graph_nodes=[{"type": "Service", "name": "rx-order", "service": "rx-order"}],
    )
    pack = build_evidence_pack(
        "How does eligibility work?", retrieval,
        query_tokens=["eligibility", "work"],
        intent="general_search", handler="search_code",
    )
    assert pack.facts
    assert pack.graph_facts
    assert pack.confidence > 0.3
    md = pack.as_markdown()
    assert "## Facts" in md
    assert "## Graph" in md


def test_pack_without_hits_is_low_confidence():
    pack = build_evidence_pack(
        "unknown xyzzy", HybridResult(query="unknown xyzzy"),
        intent="general_search", handler="search_code",
    )
    assert pack.confidence <= 0.25
    assert "insufficient_project_evidence" in pack.gaps


def test_lexical_rank_prefers_term_overlap():
    a = RetrievedChunk(
        chunk=Chunk(id="a", repo="r", rel_path="a.java", content_type=ContentType.JAVA,
                    collection="code", text="unrelated logging configuration"),
        score=0.99, source="vector",
    )
    b = RetrievedChunk(
        chunk=Chunk(id="b", repo="r", rel_path="b.java", content_type=ContentType.JAVA,
                    collection="code", text="customer validation is performed here"),
        score=0.1, source="vector",
    )
    ranked = lexical_rank("customer validation", [a, b])
    assert ranked[0].chunk.id == "b"
