"""Agent-oriented LangGraph workflow for enterprise retrieval.

Workflow stages:
    Question
      -> Intent Detection
      -> Agent Selection
      -> Graph Retrieval
      -> Vector Retrieval
      -> Reranking
      -> Verification
      -> Final Answer
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from ..graph.base import GraphStorePort
from ..logging import get_logger
from ..retrieval.hybrid_retriever import HybridResult, HybridRetriever
from ..retrieval.rag import LLMClient, build_context_block, citations_from

log = get_logger(__name__)


class WorkflowState(TypedDict, total=False):
    """LangGraph state threaded through the MultiStageWorkflow retrieval pipeline."""

    question: str
    intent: str
    intent_confidence: float
    entities: list[str]
    selected_agent: str
    agent_reason: str
    graph_evidence: list[dict[str, Any]]
    retrieval_candidates: list
    retrieval: HybridResult
    answer: str
    citations: list[dict[str, Any]]
    verification: dict[str, Any]
    context_ok: bool
    data: dict[str, Any]


_INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("exception_rca", ("stack trace", "exception", "error", "crash", "npe", "root cause")),
    ("trace_execution", ("trace", "execution path", "call chain", "runtime flow")),
    ("impact_analysis", ("blast radius", "what breaks", "impact of", "if i change")),
    ("find_callers", ("who calls", "callers", "what calls", "apis call", "services call")),
    ("find_callees", ("calls out to", "callees", "what gets called", "what does")),
    ("dependency_analysis", ("dependency", "depends on", "uses", "library", "module")),
    ("version_comparison", ("compare", "release", "version", "diff between")),
    ("incident_similarity", ("incident", "postmortem", "similar issue", "outage")),
    ("architecture", ("architecture", "overview", "ecosystem", "how does the system")),
]

_ENTITY_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:Service|Controller|Repository|Entity|Exception|DTO|Client)\b"
)
_QUOTED_RE = re.compile(r"[`'\"]([^`'\"]{2,80})[`'\"]")


@dataclass(slots=True)
class AgentPlan:
    """A retrieval agent's plan: which collections/graph mode to use and why."""

    name: str
    collections: tuple[str, ...]
    graph_mode: str
    reason: str


class RetrievalAgent(Protocol):
    """Protocol for a per-intent agent that plans retrieval collections/graph mode."""

    name: str

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Return the retrieval plan (collections/graph mode/reason) for this agent."""
        ...


class CodeRetrievalAgent:
    """Retrieval agent for code/symbol-lookup intents."""

    name = "Code Retrieval Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan retrieval scoped to code/semantic/docs/architecture collections."""
        return AgentPlan(
            name=self.name,
            collections=("semantic", "code", "docs", "architecture"),
            graph_mode="symbol",
            reason="Focus on method/class/source retrieval.",
        )


class DependencyAnalysisAgent:
    """Retrieval agent for service/module dependency-impact intents."""

    name = "Dependency Analysis Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan retrieval scoped to architecture/dependency-graph evidence."""
        return AgentPlan(
            name=self.name,
            collections=("semantic", "architecture", "code", "docs"),
            graph_mode="dependency",
            reason="Focus on service/module dependency graph evidence.",
        )


class ExecutionFlowAgent:
    """Retrieval agent for endpoint-to-downstream execution-flow intents."""

    name = "Execution Flow Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan retrieval scoped to endpoint/method execution-flow evidence."""
        return AgentPlan(
            name=self.name,
            collections=("semantic", "code", "architecture", "docs"),
            graph_mode="execution",
            reason="Focus on endpoint -> method -> downstream flow evidence.",
        )


class IncidentAnalysisAgent:
    """Retrieval agent for exception/incident/RCA intents."""

    name = "Incident Analysis Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan retrieval scoped to incidents/defects/exception evidence."""
        return AgentPlan(
            name=self.name,
            collections=("incidents", "semantic", "defects", "code"),
            graph_mode="incident",
            reason="Focus on exception, incident, and RCA evidence.",
        )


class VersionAnalysisAgent:
    """Retrieval agent for release/version comparison intents."""

    name = "Version Analysis Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan retrieval scoped to release/version-comparison evidence."""
        return AgentPlan(
            name=self.name,
            collections=("semantic", "code", "docs", "architecture"),
            graph_mode="version",
            reason="Focus on release/version comparison evidence.",
        )


class AnswerVerificationAgent:
    """General-purpose retrieval agent used to gather grounding evidence for verification."""

    name = "Answer Verification Agent"

    def plan(self, intent: str, question: str) -> AgentPlan:
        """Plan broad retrieval across all collections for grounding/verification."""
        return AgentPlan(
            name=self.name,
            collections=("semantic", "code", "docs", "architecture", "incidents", "defects"),
            graph_mode="symbol",
            reason="General verification-oriented retrieval.",
        )


def detect_intent(question: str) -> tuple[str, float]:
    """Classify a question's intent via keyword rules; return (intent, confidence)."""
    q = question.lower()
    for intent, keywords in _INTENT_RULES:
        if any(kw in q for kw in keywords):
            return intent, 0.9
    return "general_search", 0.35


def extract_entities(question: str) -> list[str]:
    """Extract likely class/method/symbol names (CamelCase or quoted) from a question."""
    entities = _ENTITY_RE.findall(question)
    entities += _QUOTED_RE.findall(question)
    seen: set[str] = set()
    out = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


class MultiStageWorkflow:
    """LangGraph workflow using explicit specialized retrieval agents."""

    def __init__(self, retriever: HybridRetriever, llm: LLMClient) -> None:
        self._retriever = retriever
        self._llm = llm
        self._agents: dict[str, RetrievalAgent] = {
            "code": CodeRetrievalAgent(),
            "dependency": DependencyAnalysisAgent(),
            "execution": ExecutionFlowAgent(),
            "incident": IncidentAnalysisAgent(),
            "version": VersionAnalysisAgent(),
            "verify": AnswerVerificationAgent(),
        }
        self._graph_app = self._build()

    def run(self, question: str) -> WorkflowState:
        """Run the full multi-stage retrieval workflow for one question."""
        return self._graph_app.invoke({"question": question})

    def _build(self):
        g = StateGraph(WorkflowState)
        g.add_node("intent_detection", self._intent_detection)
        g.add_node("agent_selection", self._agent_selection)
        g.add_node("graph_retrieval", self._graph_retrieval)
        g.add_node("vector_retrieval", self._vector_retrieval)
        g.add_node("reranking", self._reranking)
        g.add_node("verification", self._verification)
        g.add_node("final_answer", self._final_answer)
        g.add_edge(START, "intent_detection")
        g.add_edge("intent_detection", "agent_selection")
        g.add_edge("agent_selection", "graph_retrieval")
        g.add_edge("graph_retrieval", "vector_retrieval")
        g.add_edge("vector_retrieval", "reranking")
        g.add_edge("reranking", "verification")
        g.add_edge("verification", "final_answer")
        g.add_edge("final_answer", END)
        return g.compile()

    def _intent_detection(self, state: WorkflowState) -> WorkflowState:
        intent, confidence = detect_intent(state["question"])
        entities = extract_entities(state["question"])
        return {"intent": intent, "intent_confidence": confidence, "entities": entities}

    def _agent_selection(self, state: WorkflowState) -> WorkflowState:
        intent = state.get("intent", "general_search")
        if intent in ("exception_rca", "incident_similarity"):
            agent = self._agents["incident"]
        elif intent in ("trace_execution", "find_callers", "find_callees"):
            agent = self._agents["execution"]
        elif intent == "dependency_analysis":
            agent = self._agents["dependency"]
        elif intent == "version_comparison":
            agent = self._agents["version"]
        elif intent == "architecture":
            agent = self._agents["dependency"]
        else:
            agent = self._agents["code"]

        plan = agent.plan(intent, state["question"])
        return {
            "selected_agent": plan.name,
            "agent_reason": plan.reason,
            "data": {"agent_plan": {"collections": list(plan.collections), "graph_mode": plan.graph_mode}},
        }

    def _graph_retrieval(self, state: WorkflowState) -> WorkflowState:
        graph: GraphStorePort = self._retriever.graph
        plan = (state.get("data") or {}).get("agent_plan", {})
        mode = plan.get("graph_mode", "symbol")
        evidence: list[dict[str, Any]] = []
        targets = state.get("entities") or [state["question"]]

        for target in targets[:6]:
            matches = graph.find_nodes(target, limit=6)
            for m in matches:
                evidence.append({"source": "entity_match", "node": m})
                if mode in ("dependency", "version"):
                    for nb in graph.neighbors(m["id"], direction="out")[:8]:
                        evidence.append({"source": "dependency", "node": nb.get("node"), "via": nb.get("edge_type")})
                if mode == "execution":
                    for nb in graph.neighbors(m["id"], direction="in")[:8]:
                        evidence.append({"source": "caller", "node": nb.get("node"), "via": nb.get("edge_type")})
                    for nb in graph.neighbors(m["id"], direction="out")[:8]:
                        evidence.append({"source": "callee", "node": nb.get("node"), "via": nb.get("edge_type")})
                if mode == "incident":
                    impact = graph.impacted(m["id"], max_hops=3)
                    evidence.append({
                        "source": "incident_impact",
                        "node": m,
                        "impacted_services": impact.get("services", []),
                        "total_impacted": impact.get("total_impacted", 0),
                    })

        return {"graph_evidence": evidence[:60]}

    def _vector_retrieval(self, state: WorkflowState) -> WorkflowState:
        plan = (state.get("data") or {}).get("agent_plan", {})
        collections = tuple(plan.get("collections", ["code", "docs", "architecture", "defects", "incidents"]))
        candidate_limit = max(self._retriever._settings.rerank_candidate_k, 24)
        result = self._retriever.retrieve(state["question"], collections=collections, top_k=candidate_limit)
        return {"retrieval_candidates": list(result.chunks), "retrieval": result}

    def _reranking(self, state: WorkflowState) -> WorkflowState:
        candidates = list(state.get("retrieval_candidates") or [])
        if not candidates:
            return {}
        top_k = (self._retriever._settings.static.get("retrieval", {}) or {}).get("rerank_top_k", 8)
        reranked = self._retriever._reranker.rerank(state["question"], candidates, top_k)
        current: HybridResult = state["retrieval"]
        updated = HybridResult(
            query=current.query,
            chunks=reranked,
            graph_nodes=current.graph_nodes,
            services=current.services,
        )
        return {"retrieval": updated}

    def _verification(self, state: WorkflowState) -> WorkflowState:
        answer_probe = self._extractive_answer(state)
        camel_case_re = re.compile(r"\b[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b")
        evidence_terms: set[str] = set()
        for rc in state["retrieval"].chunks:
            evidence_terms |= set(camel_case_re.findall(rc.chunk.text))
        for e in state.get("graph_evidence", []):
            node = e.get("node") or {}
            if node.get("name"):
                evidence_terms.add(str(node["name"]))

        answer_terms = set(camel_case_re.findall(answer_probe))
        ratio = (len(answer_terms & evidence_terms) / len(answer_terms)) if answer_terms else 1.0
        verification = {
            "grounded_terms_ratio": round(ratio, 2),
            "ungrounded_terms": sorted(answer_terms - evidence_terms)[:12],
            "possible_hallucination": ratio < 0.3 and bool(answer_terms),
        }
        context_ok = bool(state["retrieval"].chunks) or bool(state.get("graph_evidence"))
        return {"verification": verification, "context_ok": context_ok}

    def _final_answer(self, state: WorkflowState) -> WorkflowState:
        result: HybridResult = state["retrieval"]
        citations = citations_from(result.chunks)
        fallback = self._extractive_answer(state)
        verification = state.get("verification", {})

        if not state.get("context_ok", True):
            answer = (
                "We could not find grounded evidence for this question in the indexed "
                "code/docs/graph. Try rephrasing with a specific service, class, or endpoint name.\n\n"
                + fallback
            )
        elif not self._llm.enabled:
            answer = fallback
        else:
            context = build_context_block(result.chunks, settings=self._llm.settings)
            graph_lines = "\n".join(
                f"- [{e['source']}] {e.get('node', {}).get('type')}: {e.get('node', {}).get('name')}"
                for e in state.get("graph_evidence", []) if e.get("node")
            )
            system = (
                "You are a senior engineer answering questions about a microservice platform. "
                "Use only provided graph and retrieval evidence. If evidence is weak, say so."
            )
            user = (
                f"Question:\n{state['question']}\n\n"
                f"Intent: {state.get('intent')} ({state.get('intent_confidence', 0):.0%})\n"
                f"Selected agent: {state.get('selected_agent')}\n"
                f"Agent reason: {state.get('agent_reason')}\n\n"
                f"Graph evidence:\n{graph_lines or '(none)'}\n\n"
                f"Retrieved context:\n{context}\n\n"
                f"Verification ratio: {verification.get('grounded_terms_ratio')}"
            )
            answer = self._llm.complete(system, user) or fallback

        if verification.get("possible_hallucination"):
            answer = (
                "Low-confidence answer warning: grounding ratio is low for asserted symbols.\n\n"
                + answer
            )

        data = {
            "selected_agent": state.get("selected_agent"),
            "agent_reason": state.get("agent_reason"),
        }
        return {"answer": answer, "citations": citations, "data": data}

    @staticmethod
    def _extractive_answer(state: WorkflowState) -> str:
        result: HybridResult = state["retrieval"]
        lines = [
            f"## {state['question']}",
            "",
            f"_Intent: {state.get('intent')} | Agent: {state.get('selected_agent')}_",
            "",
        ]
        if state.get("graph_evidence"):
            lines.append("**Graph evidence:**")
            for e in state["graph_evidence"][:10]:
                node = e.get("node") or {}
                lines.append(
                    f"- [{e['source']}] {node.get('type')}: `{node.get('name')}` "
                    f"(service={node.get('service')})"
                )
            lines.append("")
        if result.chunks:
            lines.append("**Top evidence:**")
            for i, rc in enumerate(result.chunks[:6], start=1):
                c = rc.chunk
                first_line = c.text.strip().splitlines()[0][:120] if c.text.strip() else ""
                lines.append(f"{i}. `{c.repo}/{c.rel_path}` — {first_line}")
        return "\n".join(lines)
