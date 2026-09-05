"""Query understanding without an LLM.

Tokenizes the user question, reuses the MCP query router for intent, and
entity-links against known graph names (rapidfuzz when installed, substring
fallback otherwise). The expanded query is what hybrid retrieval should use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging import get_logger
from ..tools.query_router import QueryRoute, classify_question

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_@./{}-]{2,}")
_GIVEN_WHEN_THEN = re.compile(
    r"\b(given|when|then|and)\b[:\s]+(.+?)(?=\b(?:given|when|then)\b|$)",
    re.IGNORECASE | re.DOTALL,
)

_TECH_SYNONYMS: dict[str, str] = {
    "@restcontroller": "REST controller HTTP endpoint",
    "@controller": "MVC controller endpoint",
    "@service": "business service layer",
    "@repository": "data access repository",
    "@entity": "JPA entity table",
    "@transactional": "transaction boundary",
    "@kafkalistener": "Kafka consumer listener",
    "@valid": "bean validation",
    "feign": "HTTP client Feign",
    "resttemplate": "HTTP client RestTemplate",
}


@dataclass(slots=True)
class QueryUnderstanding:
    """Result of understanding a question: tokens, expansion, linked entities, and route."""

    original: str
    expanded: str
    tokens: list[str]
    linked_entities: list[tuple[str, float]] = field(default_factory=list)
    ac_clauses: list[tuple[str, str]] = field(default_factory=list)
    route: QueryRoute | None = None


def tokenize(text: str) -> list[str]:
    """Tokenize text, keeping annotations/paths/identifiers intact."""
    return [t for t in _TOKEN_RE.findall(text or "") if t.strip("-_.")]


def parse_acceptance_criteria(text: str) -> list[tuple[str, str]]:
    """Extract Given/When/Then clauses with a regex fallback (no spaCy)."""
    out: list[tuple[str, str]] = []
    for m in _GIVEN_WHEN_THEN.finditer(text or ""):
        kind = m.group(1).lower()
        body = " ".join(m.group(2).split())
        if body:
            out.append((kind, body[:240]))
    return out


def link_entities(question: str, names: list[str], *, limit: int = 8) -> list[tuple[str, float]]:
    """Fuzzy-link question text to known graph names."""
    if not question or not names:
        return []
    uniq = []
    seen: set[str] = set()
    for n in names:
        if n and n not in seen and len(n) >= 3:
            seen.add(n)
            uniq.append(n)
    try:
        from rapidfuzz import fuzz, process

        hits = process.extract(
            question, uniq, scorer=fuzz.WRatio, limit=limit, score_cutoff=80,
        )
        return [(name, float(score) / 100.0) for name, score, _ in hits]
    except Exception:
        q = question.lower()
        out = []
        for n in sorted(uniq, key=len, reverse=True):
            if n.lower() in q:
                out.append((n, 0.95))
            if len(out) >= limit:
                break
        return out


def expand_query(question: str, linked: list[tuple[str, float]],
                 extra_terms: list[str] | None = None) -> str:
    """Append tech synonyms, linked entity names, and extra terms to a question for retrieval."""
    extras: list[str] = []
    qlow = (question or "").lower()
    for key, phrase in _TECH_SYNONYMS.items():
        if key in qlow:
            extras.append(phrase)
    extras.extend(name for name, _ in linked[:5])
    extras.extend(extra_terms or [])
    if not extras:
        return question
    return f"{question} {' '.join(extras)}"


# P2.4: LLM-based query rewriting for short/ambiguous queries
_REWRITE_SYSTEM = (
    "You are a search query optimizer for a Spring Boot microservices knowledge base. "
    "Rewrite the user's question as a richer search query by adding relevant technical "
    "synonyms (e.g. 'REST endpoints controllers HTTP methods', 'JPA entity table columns'). "
    "Return ONLY the rewritten query — no explanation, no quotes, no markdown."
)
_MIN_WORDS_FOR_REWRITE = 4  # skip rewrite if query is already descriptive


def llm_rewrite_query(question: str, llm) -> str:
    """Expand a short/ambiguous query using the LLM. Returns original on failure."""
    if not llm or not getattr(llm, "enabled", False):
        return question
    words = question.split()
    if len(words) >= _MIN_WORDS_FOR_REWRITE:
        return question  # already descriptive enough
    try:
        # P5.2: use async variant with 10s cap so slow Ollama startup doesn't stall routing
        rewrite_fn = getattr(llm, "complete_async", llm.complete)
        rewritten = rewrite_fn(_REWRITE_SYSTEM, question, temperature=0.0).strip()
        return rewritten if len(rewritten) > len(question) else question
    except Exception as exc:
        log.debug("query_rewrite_failed", error=str(exc))
        return question


def understand(
    question: str,
    *,
    services: list[str] | None = None,
    extra_names: list[str] | None = None,
    aliases: dict[str, str] | None = None,
    extra_terms: list[str] | None = None,
) -> QueryUnderstanding:
    """Tokenize, entity-link, route, and expand a question into a QueryUnderstanding."""
    text = (question or "").strip()
    if aliases:
        rewritten = text
        for src, dst in sorted(aliases.items(), key=lambda kv: -len(kv[0])):
            if len(src) < 4:
                continue
            pattern = re.compile(re.escape(src), re.IGNORECASE)
            if pattern.search(rewritten) and dst.lower() not in rewritten.lower():
                rewritten = pattern.sub(dst, rewritten)
        text = rewritten
    names = list(services or []) + list(extra_names or [])
    tokens = tokenize(text)
    linked = link_entities(text, names)
    route = classify_question(text, services=services or [])
    return QueryUnderstanding(
        original=question,
        expanded=expand_query(text, linked, extra_terms),
        tokens=tokens,
        linked_entities=linked,
        ac_clauses=parse_acceptance_criteria(text),
        route=route,
    )
