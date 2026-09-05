"""KnowledgeService mixin: CoreMixin (core domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...agents.langgraph_flows import ReasoningFlows
from ...agents.multi_stage_workflow import MultiStageWorkflow
from ...config import Settings, get_settings
from ...graph.factory import get_graph_store
from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...observability.langfuse_tracing import configure_langfuse
from ...observability.tracing import configure_observability, instrument_public_methods
from ...rca.incident_store import IncidentStore
from ...retrieval.hybrid_retriever import HybridRetriever
from ...retrieval.rag import LLMClient
from ...security.enforcement import instrument_with_security
from ...vector.indexer import VectorIndexer

log = get_logger(__name__)




class CoreMixin:
    """Shared init/bootstrap and cross-domain resolver/error helpers."""
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        configure_observability(self.settings)
        configure_langfuse(self.settings)
        self.graph = get_graph_store(self.settings)
        loaded = self.graph.load()
        if not loaded:
            log.warning("graph_snapshot_missing",
                        hint="run `mcp-kb-ingest` before serving")
        self.retriever = HybridRetriever(self.settings, graph=self.graph)
        self.llm = LLMClient(self.settings)
        self.flows = ReasoningFlows(self.retriever, self.llm)
        # Enterprise RCA / multi-stage retrieval additions (additive — the
        # nine original tools above are untouched and keep using `flows`).
        self.workflow = MultiStageWorkflow(self.retriever, self.llm)
        self._indexer = VectorIndexer(self.settings, store=self.retriever.store,
                                      embedder=self.retriever.embedder)
        self.incidents = IncidentStore(self.settings, graph=self.graph,
                                       indexer=self._indexer, retriever=self.retriever)
        from ...learning import get_learner
        self.learner = get_learner(self.settings)
        # P4.2: query analytics store (Postgres or JSONL fallback)
        from ...db.analytics_store import get_analytics_store
        self._analytics = get_analytics_store(self.settings)
        # Freshness/staleness surfacing for ask() — repo last-indexed lookups.
        from ...db.metadata_store import MetadataStore
        self._meta = MetadataStore(self.settings)
        # Observability: transparently wrap every public tool method with
        # latency tracking (+ OTel spans if configured) — no per-tool
        # decorator boilerplate, and new tools are covered automatically.
        instrument_public_methods(self)
        # Security: RBAC (disabled by default) + audit logging (on by
        # default), wrapped around the already-traced methods above so every
        # call is timed, access-checked, and audited in one pass.
        instrument_with_security(self, self.settings)
    def _resolve_service(self, name: str) -> str:
        services = self.graph.services()
        low = name.lower().strip()
        for s in services:
            if s.lower() == low:
                return s
        for s in services:
            if low in s.lower():
                return s
        return name
    def _resolve_entity_or_table(self, name: str) -> dict | None:
        matches = self.graph.find_nodes(
            name, {NodeType.TABLE, NodeType.ENTITY, NodeType.SERVICE,
                   NodeType.DTO, NodeType.ENDPOINT}, limit=1,
        )
        return matches[0] if matches else None
    def _not_found(self, tool: str, query: dict, term: str) -> ToolResponse:
        return ToolResponse(
            tool=tool, query=query,
            summary=f"No knowledge-graph node matched '{term}'.",
            data={"matches": []},
            markdown=(f"No entity, table or service named **{term}** was found. "
                      "Try `generate_architecture_summary` to list known services, "
                      "or re-run ingestion if the codebase changed."),
        )
