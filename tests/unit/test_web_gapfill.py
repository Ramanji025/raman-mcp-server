"""Web gap-fill trigger rules (no network)."""
from __future__ import annotations

from mcp_kb.nlp.evidence_pack import EvidencePack
from mcp_kb.retrieval.lexical import should_use_web


def test_web_skipped_when_disabled():
    assert should_use_web(
        enabled=False, confidence=0.1, min_confidence=0.45,
    ) is False


def test_web_skipped_when_confidence_high():
    assert should_use_web(
        enabled=True, confidence=0.8, min_confidence=0.45,
    ) is False


def test_web_when_confidence_low():
    assert should_use_web(
        enabled=True, confidence=0.2, min_confidence=0.45,
    ) is True


def test_web_not_for_high_confidence_project_question():
    assert should_use_web(
        enabled=True, confidence=0.7, min_confidence=0.45,
    ) is False


def test_pack_with_web_labels_sources():
    pack = EvidencePack(question="what is resilience4j", confidence=0.2, gaps=["insufficient_project_evidence"])
    updated = pack.with_web([{
        "title": "Resilience4j", "url": "https://resilience4j.readme.io",
        "snippet": "Fault tolerance library for Java.",
    }])
    assert updated.web_facts
    assert updated.web_facts[0].source.startswith("web:")
    assert "insufficient_project_evidence" not in updated.gaps
    assert "web:" in updated.as_markdown()
