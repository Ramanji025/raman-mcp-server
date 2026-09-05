"""LangGraph flows for defect analysis and feature implementation.

Each flow is a small deterministic state machine:
    retrieve -> reason (graph) -> synthesize -> assemble
LangGraph gives us explicit, inspectable steps and easy extension points
(e.g. adding a re-retrieval loop) while keeping accuracy the priority.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..logging import get_logger
from ..models import NodeType
from ..retrieval.hybrid_retriever import HybridResult, HybridRetriever
from ..retrieval.rag import LLMClient, citations_from, synthesize

log = get_logger(__name__)


class FlowState(TypedDict, total=False):
    """LangGraph state threaded through the ReasoningFlows retrieve/reason/synthesize stages."""

    question: str
    intent: str            # defect | feature | flow
    retrieval: HybridResult
    graph_context: list[dict]
    impacted: dict[str, Any]
    answer: str
    citations: list[dict]
    data: dict[str, Any]


_DEFECT_SYSTEM = (
    "You are a senior backend engineer performing root-cause defect analysis on a "
    "Spring Boot microservice platform. Identify the likely faulty service(s), the "
    "code paths involved, probable root cause, and concrete remediation steps."
)
_FEATURE_SYSTEM = (
    "You are a principal engineer producing an implementation plan for a new feature "
    "in a Spring Boot microservice platform. Specify affected services, new/changed "
    "endpoints, DTOs, entities, tables, events, and a step-by-step plan with risks."
)
_FLOW_SYSTEM = (
    "You are a solutions architect explaining an end-to-end business flow across "
    "Spring Boot microservices, naming the services, endpoints, events and tables "
    "involved and the order in which they participate."
)


class ReasoningFlows:
    """Builds and runs the compiled LangGraph flows."""

    def __init__(self, retriever: HybridRetriever, llm: LLMClient) -> None:
        self._retriever = retriever
        self._llm = llm
        self._defect = self._build(_DEFECT_SYSTEM, self._defect_reason)
        self._feature = self._build(_FEATURE_SYSTEM, self._feature_reason)
        self._flow = self._build(_FLOW_SYSTEM, self._flow_reason)

    # ---- public runners ---- #
    def analyze_defect(self, problem: str) -> FlowState:
        """Run the defect-analysis reasoning flow for a problem statement."""
        return self._defect.invoke({"question": problem, "intent": "defect"})

    def implement_feature(self, requirement: str) -> FlowState:
        """Run the feature-implementation reasoning flow for a requirement."""
        return self._feature.invoke({"question": requirement, "intent": "feature"})

    def trace_flow(self, flow_name: str) -> FlowState:
        """Run the business-flow-tracing reasoning flow for a named flow."""
        return self._flow.invoke({"question": flow_name, "intent": "flow"})

    # ---- graph construction ---- #
    def _build(self, system_prompt: str, reason_fn):
        graph = StateGraph(FlowState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("reason", reason_fn)
        graph.add_node("synthesize", self._make_synthesize(system_prompt))
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "reason")
        graph.add_edge("reason", "synthesize")
        graph.add_edge("synthesize", END)
        return graph.compile()

    # ---- shared nodes ---- #
    def _retrieve(self, state: FlowState) -> FlowState:
        collections = ("code", "docs", "architecture", "defects", "incidents")
        result = self._retriever.retrieve(state["question"], collections=collections)
        return {"retrieval": result, "graph_context": result.graph_nodes}

    def _make_synthesize(self, system_prompt: str):
        def _synthesize(state: FlowState) -> FlowState:
            result: HybridResult = state["retrieval"]
            fallback = _extractive_summary(state, result)
            answer = synthesize(
                self._llm, state["question"], result,
                system_prompt=system_prompt, fallback=fallback,
            )
            return {"answer": answer, "citations": citations_from(result.chunks)}

        return _synthesize

    # ---- intent-specific reasoning ---- #
    def _defect_reason(self, state: FlowState) -> FlowState:
        result: HybridResult = state["retrieval"]
        impacted: dict[str, Any] = {}
        # If a specific entity/table/service is implicated, compute blast radius.
        for node in state.get("graph_context", []):
            if node.get("type") in (NodeType.TABLE.value, NodeType.ENTITY.value,
                                    NodeType.SERVICE.value):
                impacted = self._retriever.graph.impacted(node["id"], max_hops=2)
                break
        return {"impacted": impacted,
                "data": {"suspect_services": result.services, "impacted": impacted}}

    def _feature_reason(self, state: FlowState) -> FlowState:
        result: HybridResult = state["retrieval"]
        touchpoints = {
            "services": result.services,
            "endpoints": [n for n in state.get("graph_context", [])
                          if n.get("type") == NodeType.ENDPOINT.value][:10],
            "entities": [n for n in state.get("graph_context", [])
                         if n.get("type") == NodeType.ENTITY.value][:10],
        }
        return {"data": {"touchpoints": touchpoints}}

    def _flow_reason(self, state: FlowState) -> FlowState:
        graph = self._retriever.graph
        endpoints = [n for n in state.get("graph_context", [])
                     if n.get("type") == NodeType.ENDPOINT.value]
        topics = [n for n in state.get("graph_context", [])
                  if n.get("type") == NodeType.TOPIC.value]
        return {"data": {
            "participating_services": state["retrieval"].services,
            "endpoints": endpoints[:12],
            "events": topics[:12],
            "service_dependencies": graph.service_dependency_map(),
        }}


def _extractive_summary(state: FlowState, result: HybridResult) -> str:
    """Deterministic answer used when no LLM is configured."""
    lines = [f"## Analysis: {state['question']}", ""]
    if result.services:
        lines.append(f"**Relevant services:** {', '.join(result.services)}")
    nodes = state.get("graph_context", [])
    if nodes:
        lines.append("\n**Knowledge-graph touchpoints:**")
        for n in nodes[:12]:
            lines.append(f"- {n.get('type')}: `{n.get('name')}` "
                         f"(service={n.get('service')})")
    lines.append("\n**Top evidence:**")
    for i, rc in enumerate(result.chunks[:6], start=1):
        c = rc.chunk
        lines.append(f"{i}. `{c.repo}/{c.rel_path}` "
                     f"— {c.text.strip().splitlines()[0][:120] if c.text.strip() else ''}")
    lines.append("\n> LLM synthesis disabled (set LLM_PROVIDER + OPENAI_API_KEY "
                 "for narrative answers). Facts above are retrieved directly from "
                 "the indexed code and docs.")
    return "\n".join(lines)
