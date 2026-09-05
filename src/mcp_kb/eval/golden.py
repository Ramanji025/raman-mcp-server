"""Golden-question retrieval eval (offline scoring of an evidence pack)."""
from __future__ import annotations

import re
from typing import Any

GOLDEN_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "eligibility",
        "question": "How does eligibility checking work?",
        "must_contain_any": ["eligib", "validat", "customer"],
        "intent": "general_search",
    },
    {
        "id": "annotation_transactional",
        "question": "What is @Transactional?",
        "must_contain_any": ["transaction", "@transactional", "rollback"],
        "intent": "technology",
    },
    {
        "id": "architecture",
        "question": "Give me a whole-system architecture overview",
        "must_contain_any": ["service", "depend"],
        "intent": "explain_architecture",
    },
    {
        "id": "annotation_autowired_override",
        "question": "How can I override the @Autowired annotation behavior?",
        "must_contain_any": ["autowired", "@autowired", "qualifier", "inject"],
        "intent": "technology",
    },
    {
        "id": "annotation_transactional_propagation",
        "question": "Explain @Transactional propagation levels",
        "must_contain_any": ["propagation", "transaction"],
        "intent": "technology",
    },
    {
        "id": "spring_boot_starter",
        "question": "What does a Spring Boot starter dependency do?",
        "must_contain_any": ["starter", "spring boot", "dependency", "auto-config", "autoconfig"],
        "intent": "technology",
    },
    {
        "id": "kafka_topology",
        "question": "Show me the Kafka topology across services",
        "must_contain_any": ["kafka", "topic", "producer", "consumer"],
        "intent": "kafka_topology",
    },
    {
        "id": "impact_analysis",
        "question": "What is the blast radius of changing the Order entity?",
        "must_contain_any": ["order", "impact", "depend"],
        "intent": "impact_analysis",
    },
    {
        "id": "defect_ticket",
        "question": "What changed for ticket JIRA-1234?",
        "must_contain_any": ["jira-1234", "change", "commit"],
        "intent": "defect_changes",
    },
    {
        "id": "business_flow",
        "question": "Trace the business flow for order placement end to end",
        "must_contain_any": ["order", "flow", "service"],
        "intent": "trace_business_flow",
    },
    {
        "id": "security_audit",
        "question": "Run a security audit on the access management service",
        "must_contain_any": ["access", "security", "auth"],
        "intent": "security_audit",
    },
    {
        "id": "best_practices_repository",
        "question": "What are the best practices for Spring Data repositories?",
        "must_contain_any": ["repository", "jparepository", "best practice", "query"],
        "intent": "technology",
    },
    {
        "id": "onboarding",
        "question": "I'm new to the team, how do I get started with the asset service?",
        "must_contain_any": ["asset", "service", "onboard"],
        "intent": "onboarding",
    },
    {
        "id": "find_endpoint",
        "question": "Find the endpoint for uploading a device inspection file",
        "must_contain_any": ["upload", "inspect", "endpoint", "/"],
        "intent": "find_endpoint",
    },
    {
        "id": "exception_rca",
        "question": "Why would a NullPointerException occur in the fleet management service?",
        "must_contain_any": ["null", "exception", "fleet"],
        "intent": "exception_rca",
    },
]

# Tokens that, if present in the final answer but ABSENT from the underlying
# evidence pack, indicate the LLM likely invented a fact rather than only
# rephrasing the pack (the extractive-answer contract this platform relies on).
_ENTITY_TOKEN_RE = re.compile(r"\b[A-Z][a-zA-Z]{3,}(?:Service|Controller|Repository|Entity)\b")



def score_answer(answer: str, golden: dict[str, Any]) -> dict[str, Any]:
    """Lexical hit-rate against required substrings (case-insensitive)."""
    blob = (answer or "").lower()
    needles = [n.lower() for n in golden.get("must_contain_any") or []]
    hits = [n for n in needles if n in blob]
    ok = bool(hits) if needles else bool(blob.strip())
    return {
        "id": golden.get("id"),
        "passed": ok,
        "hits": hits,
        "missing": [n for n in needles if n not in hits],
    }


def score_pack_markdown(markdown: str, questions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score one concatenated answer blob against every golden item (unit/offline)."""
    results = [score_answer(markdown, g) for g in (questions or GOLDEN_QUESTIONS)]
    passed = sum(1 for r in results if r["passed"])
    return {
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "results": results,
    }


def score_faithfulness(answer: str, pack_markdown: str) -> dict[str, Any]:
    """Deterministic guardrail for the extractive-pack contract: flag
    Service/Controller/Repository/Entity-shaped names the LLM's final answer
    mentions that never appear anywhere in its own evidence pack — i.e. facts
    it likely invented rather than rephrased. This does not require an LLM
    judge and can run in CI on every eval pass.
    """
    answer_entities = set(_ENTITY_TOKEN_RE.findall(answer or ""))
    pack_blob = pack_markdown or ""
    unsupported = sorted(e for e in answer_entities if e not in pack_blob)
    return {
        "answer_entities": sorted(answer_entities),
        "unsupported_entities": unsupported,
        "faithful": not unsupported,
    }
