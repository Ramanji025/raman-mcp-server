"""KnowledgeService mixin: SearchMixin (search domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class SearchMixin:
    """search_domain_knowledge tool."""
    def search_domain_knowledge(self, query: str) -> ToolResponse:
        """Hybrid-retrieval search across docs, architecture, incidents, and code."""
        retrieval = self.retriever.retrieve(
            query, collections=("docs", "architecture", "incidents", "code"),
        )
        results = [{
            "repo": rc.chunk.repo, "path": rc.chunk.rel_path,
            "service": rc.chunk.metadata.get("service"),
            "score": round(rc.score, 4),
            "excerpt": rc.chunk.text.strip()[:400],
        } for rc in retrieval.chunks]
        data = {"results": results, "services": retrieval.services,
                "graph_nodes": retrieval.graph_nodes[:15]}
        md = self._render_search_md(query, retrieval)
        return ToolResponse(
            tool="search_domain_knowledge", query={"query": query},
            summary=f"{len(results)} relevant passages across {len(retrieval.services)} services.",
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    @staticmethod
    def _render_search_md(query: str, r: HybridResult) -> str:
        lines = [f"# Domain knowledge: {query}", ""]
        for i, rc in enumerate(r.chunks[:8], start=1):
            c = rc.chunk
            lines.append(f"**[{i}] `{c.repo}/{c.rel_path}`** (score {rc.score:.3f})")
            lines.append(f"> {c.text.strip()[:300]}\n")
        return "\n".join(lines)
