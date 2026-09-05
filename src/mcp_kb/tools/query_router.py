"""Natural-language routing for the `ask` MCP tool.

MCP clients (Open WebUI slash menus, IDE agents) cannot enumerate every user
question as a separate tool.  This module classifies free text and maps it to
an existing knowledge-engine handler.  Unmatched questions fall through to
hybrid retrieval over the project graph/index plus technology concepts.

P2.1: embedding-based intent classifier replaces keyword-first-match.
Keyword rules are kept as a fast tie-breaker for exact phrase hits.
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field

from ..logging import get_logger

log = get_logger(__name__)

# Rally / Azure-style ticket ids used by get_defect_changes.
_TICKET_RE = re.compile(r"\b(?:DE|US|TA|F|S)\d{4,}\b", re.IGNORECASE)
_ENDPOINT_RE = re.compile(r"(?:/(?:api|v\d+)(?:/[\w\-{}]+){1,}|/[a-z][\w\-]{2,}(?:/[\w\-{}]+){1,})")

# --------------------------------------------------------------------------- #
# P2.1: Intent descriptions for embedding-based classification.
# Each entry: (intent_name, description_used_for_embedding)
# --------------------------------------------------------------------------- #
_INTENT_DESCRIPTIONS: list[tuple[str, str]] = [
    ("defect_changes",      "review a defect ticket, show what changed in a story or defect, defect fix commit history"),
    ("analyze_defect",      "root cause analysis of a bug or incident, production issue, outage, failing service, not working"),
    ("exception_rca",       "analyze a stack trace, NullPointerException, exception crash, runtime error"),
    ("implement_feature",   "implement a new feature, add functionality, user story requirement, implementation plan"),
    ("impact_analysis",     "blast radius, what breaks if I change this, cross-service impact analysis"),
    ("trace_execution",     "trace execution path, call chain, runtime flow, execution flow of a method"),
    ("find_endpoint",       "find REST API endpoint, which API handles this, HTTP method and path"),
    ("trace_business_flow", "end-to-end business flow across services, fulfillment flow, cross-service business process"),
    ("onboarding",          "onboarding a new developer, knowledge transfer, full KT, where to start"),
    ("security_audit",      "security audit, unprotected endpoints, hardcoded secrets, CVE vulnerabilities"),
    ("kafka_topology",      "Kafka event topology, producer consumer flow, Kafka topics and events"),
    ("find_callers",        "who calls this method, all callers of a function, what invokes this"),
    ("find_callees",        "what does this method call, callees, outbound calls from a method"),
    ("explain_architecture","system architecture, whole ecosystem, all services, dependency diagram"),
    ("inspect_application", "full platform inspection, application scorecard, platform health audit"),
    ("inspect_service",     "service scorecard, service health check, audit one microservice"),
    ("explain_service",     "explain a microservice, overview of a service, what does this service do"),
]

# --------------------------------------------------------------------------- #
# Singleton embedding classifier — lazy-loaded on first classification call.
# --------------------------------------------------------------------------- #
class _IntentClassifier:
    """Cosine-similarity classifier over pre-embedded intent descriptions."""

    _instance: _IntentClassifier | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._intents: list[str] = []
        self._vecs: list[list[float]] = []
        self._ready = False

    @classmethod
    def get(cls) -> _IntentClassifier:
        """Return the lazily-built process-wide singleton classifier."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = cls()
                    inst._build()
                    cls._instance = inst
        return cls._instance

    def _build(self) -> None:
        try:
            from fastembed import TextEmbedding
            model = TextEmbedding("BAAI/bge-small-en-v1.5")  # lightweight: only used for routing
            texts = [desc for _, desc in _INTENT_DESCRIPTIONS]
            vecs = [v.tolist() for v in model.embed(texts)]
            self._intents = [intent for intent, _ in _INTENT_DESCRIPTIONS]
            self._vecs = vecs
            self._ready = True
        except Exception as exc:
            log.debug("intent_classifier_build_failed", error=str(exc))
            self._ready = False

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-9)

    def classify(self, query: str) -> tuple[str, float] | None:
        """Return (intent, confidence) or None if classifier not ready."""
        if not self._ready:
            return None
        try:
            from fastembed import TextEmbedding
            model = TextEmbedding("BAAI/bge-small-en-v1.5")
            qvec = next(iter(model.query_embed([query]))).tolist()
            scores = [(intent, self._cosine(qvec, vec))
                      for intent, vec in zip(self._intents, self._vecs)]
            best = max(scores, key=lambda x: x[1])
            return best if best[1] >= 0.55 else None
        except Exception as exc:
            log.debug("intent_classify_failed", error=str(exc))
            return None


# --------------------------------------------------------------------------- #
# P2.3: Per-intent collection scoping — prevents noise from unrelated indexes.
# Fallback (None) means search all collections.
# --------------------------------------------------------------------------- #
_COLLECTION_MAP: dict[str, tuple[str, ...]] = {
    "explain_service":      ("code", "docs", "architecture"),
    "explain_architecture": ("architecture", "docs"),
    "find_endpoint":        ("code", "docs"),
    "analyze_defect":       ("code", "defects", "incidents"),
    "exception_rca":        ("code", "defects", "incidents"),
    "trace_business_flow":  ("code", "architecture"),
    "impact_analysis":      ("code", "architecture"),
    "trace_execution":      ("code",),
    "kafka_topology":       ("code", "architecture"),
    "security_audit":       ("code", "docs"),
    "implement_feature":    ("code", "docs", "architecture"),
    "onboarding":           ("docs", "architecture", "code"),
    "find_callers":         ("code",),
    "find_callees":         ("code",),
}

# P2.2: intents that benefit from deeper graph traversal (3 hops instead of 2)
_DEEP_HOP_INTENTS: frozenset[str] = frozenset({
    "trace_business_flow", "impact_analysis", "cross_service_impact",
    "analyze_defect", "trace_execution",
})

# Broad, high-blast-radius intents ('inspect the whole platform') that generic
# paraphrases ('how does X work in our platform?') embed close to; require a
# much higher embedding-confidence bar for these so they only fire on
# near-exact semantic matches, not everyday questions mentioning "platform"/"service".
_EMBED_HIGH_BAR_INTENTS: frozenset[str] = frozenset({"inspect_application", "inspect_service"})
_EMBED_HIGH_BAR_CONFIDENCE = 0.8


def collections_for_intent(intent: str) -> tuple[str, ...] | None:
    """Return Qdrant collections to search, or None to search all."""
    return _COLLECTION_MAP.get(intent)


def graph_hops_for_intent(intent: str, base_hops: int = 2) -> int:
    """Return graph expansion hops for a given intent."""
    return base_hops + 1 if intent in _DEEP_HOP_INTENTS else base_hops


# Ordered: first match wins — kept as fast exact-phrase boost on top of embeddings.
_ROUTE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("defect_changes", (
        "review defect", "review ticket", "review story", "what changed in",
        "show me the fix", "defect changes", "ticket changes",
    )),
    ("analyze_defect", (
        "root cause", "defect", "bug", "incident", "outage", "stuck in",
        "failing", "not working", "production issue",
    )),
    ("exception_rca", (
        "stack trace", "nullpointer", "npe", "exception", "crash",
    )),
    ("implement_feature", (
        "implement", "add feature", "new feature", "user story", "requirement",
        "we need to add", "i need to add", "plan the implementation",
    )),
    ("impact_analysis", (
        "blast radius", "what breaks", "impact of", "if i change",
        "if i add", "what is impacted", "cross-service impact",
    )),
    ("trace_execution", (
        "execution path", "call chain", "trace the", "runtime flow",
        "execution flow",
    )),
    ("find_endpoint", (
        "endpoint", "rest api", "find api", "which api", "api path",
        "http method",
    )),
    ("trace_business_flow", (
        "business flow", "end to end", "end-to-end", "fulfilment",
        "fulfillment", "across services",
    )),
    ("onboarding", (
        "onboard", "new developer", "knowledge transfer", "where do i start",
        "i'm new", "im new", "full kt",
    )),
    ("security_audit", (
        "security audit", "unprotected", "hardcoded secret", "cve",
    )),
    ("kafka_topology", (
        "kafka topology", "event topology", "kafka topic", "kafka producer",
        "kafka consumer",
    )),
    ("find_callers", (
        "who calls", "callers of", "what calls", "apis call",
    )),
    ("find_callees", (
        "calls out to", "callees", "what gets called",
    )),
    ("explain_architecture", (
        "architecture", "ecosystem", "whole system", "all services",
        "dependency diagram", "how does the system",
    )),
    ("inspect_application", (
        "inspect the application", "inspect the platform", "inspection bible",
        "application scorecard", "platform health", "inspect everything",
    )),
    ("inspect_service", (
        "inspect service", "inspect this service", "service scorecard",
        "service health", "audit this service",
    )),
    ("explain_service", (
        "explain service", "overview of", "tell me about", "what does",
        "service overview",
    )),
]


@dataclass(frozen=True, slots=True)
class QueryRoute:
    """Decision produced by :func:`classify_question`."""

    intent: str
    handler: str
    confidence: float
    service_name: str | None = None
    ticket_id: str | None = None
    endpoint_path: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def mentioned_services(question: str, services: list[str]) -> list[str]:
    """Longest-first substring match against indexed service names."""
    q = question.lower()
    hits: list[str] = []
    for name in sorted(services, key=len, reverse=True):
        key = name.lower()
        if len(key) < 3:
            continue
        if key in q:
            hits.append(name)
    return hits


def classify_question(question: str, *, services: list[str] | None = None) -> QueryRoute:
    """Map natural language to an internal handler.

    P2.1: tries keyword rules first (high-precision exact phrases), then falls
    back to the embedding-based intent classifier (higher recall for paraphrases),
    then defaults to hybrid search.
    """
    text = (question or "").strip()
    if not text:
        return QueryRoute(
            intent="empty", handler="search_code", confidence=0.0,
            reasons=("empty question",),
        )

    q = text.lower()
    reasons: list[str] = []
    ticket = _TICKET_RE.search(text)
    endpoint = _ENDPOINT_RE.search(text)
    svc_hits = mentioned_services(text, services or [])
    service_name = svc_hits[0] if svc_hits else None

    if ticket:
        reasons.append(f"ticket {ticket.group(0)}")
        return QueryRoute(
            intent="defect_changes", handler="get_defect_changes",
            confidence=0.95,
            ticket_id=ticket.group(0).upper(),
            service_name=service_name, reasons=tuple(reasons),
        )

    # --- keyword pass (exact phrases, high precision) ---
    matched_intent: str | None = None
    for intent, phrases in _ROUTE_RULES:
        if any(p in q for p in phrases):
            matched_intent = intent
            reasons.append(f"phrase:{intent}")
            break

    if matched_intent == "defect_changes" and not ticket:
        matched_intent = "analyze_defect"

    # --- P2.1: embedding classifier fallback when no keyword match or low-confidence intent ---
    if matched_intent is None:
        embed_result = _IntentClassifier.get().classify(text)
        if embed_result:
            embed_intent, embed_conf = embed_result
            if embed_intent in _EMBED_HIGH_BAR_INTENTS and embed_conf < _EMBED_HIGH_BAR_CONFIDENCE:
                reasons.append(f"embed_rejected:{embed_intent}:{embed_conf:.2f}")
            else:
                matched_intent = embed_intent
                reasons.append(f"embed:{matched_intent}:{embed_conf:.2f}")

    handler, confidence = _handler_for(
        matched_intent, service_name=service_name, endpoint=bool(endpoint),
    )

    if matched_intent is None and endpoint:
        handler, confidence = "find_endpoint", 0.8
        reasons.append("endpoint path")
        matched_intent = "find_endpoint"

    if matched_intent is None and service_name and _looks_like_service_overview(q):
        handler, confidence = "explain_service", 0.75
        reasons.append("service name")
        matched_intent = "explain_service"

    if matched_intent is None:
        handler, confidence = "search_code", 0.4
        reasons.append("general hybrid search")
        matched_intent = "general_search"

    return QueryRoute(
        intent=matched_intent, handler=handler, confidence=confidence,
        service_name=service_name,
        ticket_id=ticket.group(0).upper() if ticket else None,
        endpoint_path=endpoint.group(0) if endpoint else None,
        reasons=tuple(reasons),
    )


def _looks_like_service_overview(q: str) -> bool:
    return any(w in q for w in (
        "explain", "overview", "about", "describe", "what is", "how does",
        "responsibilities", "depends",
    ))


def _handler_for(
    intent: str | None, *, service_name: str | None, endpoint: bool,
) -> tuple[str, float]:
    """Return (handler, confidence)."""
    if intent is None:
        return "search_code", 0.4
    mapping: dict[str, tuple[str, float]] = {
        "analyze_defect": ("analyze_defect", 0.88),
        "exception_rca": ("analyze_defect", 0.85),
        "implement_feature": ("implement_feature", 0.88),
        "impact_analysis": ("impact_analysis", 0.88),
        "trace_execution": ("search_code", 0.8),
        "find_endpoint": ("find_endpoint", 0.88),
        "trace_business_flow": ("trace_business_flow", 0.88),
        "onboarding": ("onboarding_assistant", 0.85),
        "security_audit": ("security_audit", 0.85),
        "kafka_topology": ("kafka_topology", 0.85),
        "find_callers": ("search_code", 0.8),
        "find_callees": ("search_code", 0.8),
        "explain_architecture": (
            "explain_service" if service_name else "explain_architecture",
            0.9,
        ),
        "inspect_application": ("inspect_application", 0.92),
        "inspect_service": (
            "inspect_service" if service_name else "inspect_application",
            0.9 if service_name else 0.85,
        ),
        "explain_service": (
            "explain_service" if service_name else "search_code",
            0.85 if service_name else 0.5,
        ),
        "defect_changes": ("get_defect_changes", 0.9),
    }
    handler, conf = mapping.get(intent, ("search_code", 0.4))
    if handler == "find_endpoint" and endpoint:
        return handler, min(0.95, conf + 0.05)
    if handler in {"explain_service", "security_audit"} and not service_name:
        return "search_code", 0.45
    return handler, conf
