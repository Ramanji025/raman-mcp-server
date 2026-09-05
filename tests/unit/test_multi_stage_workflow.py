"""Unit tests for the multi-stage retrieval workflow (agents/multi_stage_workflow.py)."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from mcp_kb.agents.multi_stage_workflow import (
    MultiStageWorkflow,
    detect_intent,
    extract_entities,
)
from mcp_kb.models import Chunk, ContentType, RetrievedChunk
from mcp_kb.retrieval.reranker import NoopReranker


@pytest.mark.parametrize("question,expected_intent", [
    ("Find all callers of processPayment()", "find_callers"),
    ("What breaks if validateOrder changes?", "impact_analysis"),
    ("Which APIs call InventoryService?", "find_callers"),
    ("Trace the execution path for createOrder", "trace_execution"),
    ("What does OrderService call?", "find_callees"),
    ("We saw a NullPointerException stack trace in prod", "exception_rca"),
    ("Show implementation in release 2.7", "version_comparison"),
    ("Give me an architecture overview", "architecture"),
])
def test_intent_detection_matches_expected_category(question, expected_intent):
    intent, confidence = detect_intent(question)
    assert intent == expected_intent
    assert confidence > 0.5


def test_low_confidence_for_unmatched_generic_question():
    intent, confidence = detect_intent("Where is customer validation performed?")
    assert intent == "general_search"
    assert confidence < 0.5


def test_extract_entities_finds_camelcase_class_names():
    entities = extract_entities("What breaks if OrderService.validateOrder changes?")
    assert "OrderService" in entities


def test_extract_entities_finds_quoted_terms():
    entities = extract_entities("Find all callers of 'processPayment'")
    assert "processPayment" in entities


# --------------------------------------------------------------------------- #
# End-to-end workflow test with fakes (no live Qdrant/graph/LLM required)
# --------------------------------------------------------------------------- #
@dataclass
class _FakeHybridResult:
    query: str
    chunks: list = field(default_factory=list)
    graph_nodes: list = field(default_factory=list)
    services: list = field(default_factory=list)


class _FakeGraph:
    def find_nodes(self, text, node_types=None, limit=25):
        if "order" in text.lower():
            return [{"id": "service:rx-order:rx-order", "type": "Service",
                     "name": "rx-order", "service": "rx-order"}]
        return []

    def impacted(self, node_id, max_hops=3):
        return {"root": node_id, "levels": [], "services": ["rx-pricing"], "total_impacted": 3}

    def neighbors(self, node_id, edge_types=None, direction="out"):
        return []


class _FakeRetriever:
    def __init__(self, chunks):
        self.graph = _FakeGraph()
        self._chunks = chunks
        self._settings = SimpleNamespace(rerank_candidate_k=30, static={})
        self._reranker = NoopReranker()

    def retrieve(self, query, *, collections=(), service=None, kind=None, top_k=None):
        return _FakeHybridResult(query=query, chunks=self._chunks,
                                  services=["rx-order"] if self._chunks else [])


class _FakeLLM:
    enabled = False

    def complete(self, system, user):
        return ""


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=Chunk(id="c1", repo="rx-order", rel_path="OrderService.java",
                    content_type=ContentType.JAVA, collection="code", text=text),
        score=0.9,
    )


def test_workflow_end_to_end_with_evidence_produces_grounded_answer():
    retriever = _FakeRetriever([_chunk("class OrderService { void validateOrder() {} }")])
    workflow = MultiStageWorkflow(retriever, _FakeLLM())
    state = workflow.run("What breaks if OrderService changes?")
    assert state["intent"] == "impact_analysis"
    assert state["context_ok"] is True
    assert state["graph_evidence"]
    assert "verification" in state
    assert state["verification"]["possible_hallucination"] is False


def test_workflow_with_no_evidence_flags_low_context():
    retriever = _FakeRetriever([])
    workflow = MultiStageWorkflow(retriever, _FakeLLM())
    state = workflow.run("Tell me about ZzzNonexistentThing")
    assert state["context_ok"] is False
    assert "could not find grounded evidence" in state["answer"]
