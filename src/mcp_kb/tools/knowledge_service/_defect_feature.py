"""KnowledgeService mixin: DefectFeatureMixin (defect_feature domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import ToolResponse

log = get_logger(__name__)




class DefectFeatureMixin:
    """analyze_defect / implement_feature tools."""
    def analyze_defect(self, problem_statement: str) -> ToolResponse:
        """Root-cause a defect/incident description via the RCA reasoning flow."""
        state = self.flows.analyze_defect(problem_statement)
        data = state.get("data", {})
        return ToolResponse(
            tool="analyze_defect", query={"problem_statement": problem_statement},
            summary=(f"Likely services: "
                     f"{', '.join(data.get('suspect_services', [])) or 'unknown'}."),
            data=data, markdown=state.get("answer", ""),
            citations=state.get("citations", []),
        )
    def implement_feature(self, requirement: str) -> ToolResponse:
        """Produce an implementation plan and touchpoints for a new feature requirement."""
        state = self.flows.implement_feature(requirement)
        data = state.get("data", {})
        tp = data.get("touchpoints", {})
        return ToolResponse(
            tool="implement_feature", query={"requirement": requirement},
            summary=(f"Plan touches {len(tp.get('services', []))} services, "
                     f"{len(tp.get('endpoints', []))} endpoints."),
            data=data, markdown=state.get("answer", ""),
            citations=state.get("citations", []),
        )
