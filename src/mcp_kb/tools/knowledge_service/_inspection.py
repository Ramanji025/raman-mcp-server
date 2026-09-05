"""KnowledgeService mixin: InspectionMixin (inspection domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse

log = get_logger(__name__)




class InspectionMixin:
    """inspect_service / inspect_application scorecard tools."""
    def inspect_service(self, service_name: str) -> ToolResponse:
        """Inspection-bible scorecard for one service (extractive, graph-backed)."""
        from ...inspection.scorecard import build_report

        svc = self._resolve_service(service_name)
        snap = self._inspection_snapshot(svc)
        report = build_report(svc, snap, services=[svc])
        return ToolResponse(
            tool="inspect_service", query={"service_name": service_name},
            summary=(f"{svc}: {report.overall_grade} ({report.overall_score:.0f}/100) "
                     f"across {len(report.dimensions)} dimensions."),
            data=report.model_dump(), markdown=report.as_markdown(),
        )
    def inspect_application(self) -> ToolResponse:
        """Platform-wide inspection bible: score every indexed service and roll up."""
        from ...inspection.scorecard import build_report, merge_snaps

        services = self.graph.services()
        snaps = [self._inspection_snapshot(s) for s in services]
        merged = merge_snaps(snaps) if snaps else {}
        report = build_report("application", merged, services=services)
        md = report.as_markdown()
        if services:
            md += "\n## Per-service grades\n"
            for s, snap in zip(services, snaps):
                one = build_report(s, snap, services=[s])
                md += f"- `{s}`: **{one.overall_grade}** ({one.overall_score:.0f}/100)\n"
        return ToolResponse(
            tool="inspect_application", query={},
            summary=(f"{len(services)} services: overall {report.overall_grade} "
                     f"({report.overall_score:.0f}/100)."),
            data={**report.model_dump(), "service_count": len(services)},
            markdown=md,
        )
    def _inspection_snapshot(self, svc: str) -> dict:
        endpoints = self.graph.nodes_by_type(NodeType.ENDPOINT, svc)
        configs = self.graph.nodes_by_type(NodeType.CONFIG, svc)
        controllers = self.graph.nodes_by_type(NodeType.CONTROLLER, svc)
        layers = self.graph.nodes_by_type(NodeType.SERVICE_LAYER, svc)
        repos = self.graph.nodes_by_type(NodeType.REPOSITORY, svc)
        entities = self.graph.nodes_by_type(NodeType.ENTITY, svc)
        tests = self.graph.nodes_by_type(NodeType.TEST_CLASS, svc)
        producers = [p for p in self.graph.nodes_by_type(NodeType.KAFKA_PRODUCER)
                     if p.get("service") == svc]
        consumers = [c for c in self.graph.nodes_by_type(NodeType.KAFKA_CONSUMER)
                     if c.get("service") == svc]
        migrations = self.graph.nodes_by_type(NodeType.MIGRATION, svc)
        unprotected = sum(1 for ep in endpoints if not (ep.get("attributes") or {}).get("security"))
        secrets = 0
        for cfg in configs:
            secrets += len((cfg.get("attributes") or {}).get("hardcoded_secrets") or [])
        all_nodes = self.graph.all_nodes(svc) if hasattr(self.graph, "all_nodes") else []
        has_advice = any(
            "ControllerAdvice" in str((n.get("attributes") or {}).get("fqcn", ""))
            or "ExceptionHandler" in str(n.get("attributes") or {})
            for n in all_nodes
        )
        high = med = low = 0
        schema_ap = 0
        for node in controllers + layers + repos + entities:
            for smell in (node.get("attributes") or {}).get("antipatterns") or []:
                sev = self._smell_severity(smell)
                if sev == "HIGH":
                    high += 1
                elif sev == "MEDIUM":
                    med += 1
                else:
                    low += 1
        for ent in entities:
            schema_ap += len((ent.get("attributes") or {}).get("antipatterns") or [])
        tested = {(t.get("attributes") or {}).get("tested_class") for t in tests}
        tested.discard(None)
        untested_c = sum(1 for c in controllers if c.get("name") not in tested)
        coverage = (
            len(tested) / max(len(controllers) + len(layers) + len(repos), 1) * 100
        )
        prod_topics = {p.get("attributes", {}).get("topic") for p in producers
                       if p.get("attributes", {}).get("topic")}
        cons_topics = {c.get("attributes", {}).get("topic") for c in consumers
                       if c.get("attributes", {}).get("topic")}
        dep_map = self.graph.service_dependency_map()
        depends = dep_map.get(svc, [])
        dependents = [s for s, ds in dep_map.items() if svc in ds]
        return {
            "endpoint_count": len(endpoints),
            "unprotected_count": unprotected,
            "secret_count": secrets,
            "has_exception_handler": has_advice,
            "smells_high": high,
            "smells_medium": med,
            "smells_low": low,
            "coverage_pct": round(coverage, 1),
            "untested_controllers": untested_c,
            "entity_count": len(entities),
            "schema_antipatterns": schema_ap,
            "has_migrations": bool(migrations),
            "unmatched_producers": len(prod_topics - cons_topics),
            "unmatched_consumers": len(cons_topics - prod_topics),
            "topic_count": len(prod_topics | cons_topics),
            "depends_on_count": len(depends),
            "dependent_count": len(dependents),
        }
    @staticmethod
    def _smell_severity(smell: str) -> str:
        high_keywords = ("N+1", "hardcoded secret", "Transactional on @RestController",
                         "Missing @Valid", "God class")
        medium_keywords = ("field injection", "generic Exception", "missing @Transactional",
                           "hardcoded URL")
        for kw in high_keywords:
            if kw.lower() in smell.lower():
                return "HIGH"
        for kw in medium_keywords:
            if kw.lower() in smell.lower():
                return "MEDIUM"
        return "LOW"
