"""KnowledgeService mixin: OnboardingMixin (onboarding domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class OnboardingMixin:
    """onboarding_assistant tool."""
    def onboarding_assistant(self, topic: str) -> ToolResponse:
        """Build a reading list and suggested learning steps for a new-developer topic."""
        retrieval = self.retriever.retrieve(
            f"onboarding guide: {topic}",
            collections=("docs", "architecture", "code"),
        )
        services = retrieval.services or self.graph.services()[:5]
        steps = self._onboarding_steps(topic, services, retrieval)
        data = {"topic": topic, "suggested_services": services,
                "reading_list": [
                    {"path": f"{rc.chunk.repo}/{rc.chunk.rel_path}",
                     "why": rc.chunk.metadata.get("heading")
                            or rc.chunk.metadata.get("kind", "reference")}
                    for rc in retrieval.chunks[:8]],
                "steps": steps}
        md = self._render_onboarding_md(topic, data, retrieval)
        return ToolResponse(
            tool="onboarding_assistant", query={"topic": topic},
            summary=f"Onboarding path for '{topic}' across {len(services)} services.",
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    @staticmethod
    def _onboarding_steps(topic: str, services: list[str], r: HybridResult) -> list[str]:
        return [
            f"Read the architecture summary to see where '{topic}' fits.",
            f"Focus on these services first: {', '.join(services[:5]) or 'n/a'}.",
            "Clone the repos and run `mcp-kb-ingest` locally to explore the graph.",
            f"Use `find_endpoint` and `explain_service` to drill into '{topic}'.",
            "Review the reading list below, then trace a business flow end to end.",
        ]
    @staticmethod
    def _render_onboarding_md(topic: str, d: dict, r: HybridResult) -> str:
        lines = [f"# Onboarding: {topic}", "", "## Suggested learning path"]
        for i, step in enumerate(d["steps"], start=1):
            lines.append(f"{i}. {step}")
        lines.append("\n## Reading list")
        for item in d["reading_list"]:
            lines.append(f"- `{item['path']}` — {item['why']}")
        return "\n".join(lines)
