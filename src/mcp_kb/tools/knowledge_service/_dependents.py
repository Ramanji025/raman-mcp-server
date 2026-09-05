"""KnowledgeService mixin: DependentsMixin (dependents domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse

log = get_logger(__name__)




class DependentsMixin:
    """service_dependents / service_failure_impact tools."""
    def service_dependents(self, service_name: str) -> ToolResponse:
        """Return what depends on a service (direct + transitive)."""
        svc = self._resolve_service(service_name)
        matches = self.graph.find_nodes(svc, {NodeType.SERVICE, NodeType.GATEWAY}, limit=1)
        if not matches:
            return self._not_found("service_dependents", {"service_name": service_name}, svc)

        target = matches[0]
        impact = self.graph.impacted(target["id"], max_hops=4)
        direct = [n for n in self.graph.neighbors(target["id"], direction="in") if n.get("node")]
        deps = []
        for item in direct:
            node = item["node"]
            deps.append({
                "name": node.get("name"),
                "type": node.get("type"),
                "service": node.get("service"),
                "via": item.get("edge_type"),
            })

        data = {
            "target": {"name": target.get("name"), "id": target.get("id"), "type": target.get("type")},
            "direct_dependents": deps,
            "transitive_impacted_services": impact.get("services", []),
            "transitive_levels": impact.get("levels", []),
            "total_impacted_nodes": impact.get("total_impacted", 0),
        }
        md = self._render_service_dependents_md(data)
        return ToolResponse(
            tool="service_dependents",
            query={"service_name": service_name},
            summary=(f"{len(deps)} direct dependent node(s), "
                     f"{len(data['transitive_impacted_services'])} transitive impacted service(s)."),
            data=data,
            markdown=md,
        )
    def service_failure_impact(self, service_name: str) -> ToolResponse:
        """Return impact if a service fails: callers, impacted endpoints, and services."""
        svc = self._resolve_service(service_name)
        matches = self.graph.find_nodes(svc, {NodeType.SERVICE, NodeType.GATEWAY}, limit=1)
        if not matches:
            return self._not_found("service_failure_impact", {"service_name": service_name}, svc)

        target = matches[0]
        impact = self.graph.impacted(target["id"], max_hops=5)
        impacted_endpoints = []
        for level in impact.get("levels", []):
            for node in level:
                if node.get("type") == NodeType.ENDPOINT.value:
                    impacted_endpoints.append({
                        "name": node.get("name"),
                        "service": node.get("service"),
                    })

        data = {
            "failed_service": {"name": target.get("name"), "id": target.get("id")},
            "impacted_services": impact.get("services", []),
            "impacted_endpoints": impacted_endpoints,
            "total_impacted_nodes": impact.get("total_impacted", 0),
            "levels": impact.get("levels", []),
        }
        md = self._render_service_failure_impact_md(data)
        return ToolResponse(
            tool="service_failure_impact",
            query={"service_name": service_name},
            summary=(f"Failure impact: {len(data['impacted_services'])} service(s), "
                     f"{len(impacted_endpoints)} endpoint(s), "
                     f"{data['total_impacted_nodes']} node(s)."),
            data=data,
            markdown=md,
        )
    @staticmethod
    def _render_service_dependents_md(d: dict) -> str:
        t = d["target"]
        lines = [f"# Dependents of `{t['name']}`", ""]
        if not d["direct_dependents"]:
            lines.append("No direct dependents found.")
        else:
            lines.append("## Direct dependents")
            for dep in d["direct_dependents"][:40]:
                lines.append(f"- {dep['type']}: `{dep['name']}` (service={dep['service']}, via={dep['via']})")
        if d["transitive_impacted_services"]:
            lines.append("\n## Transitive impacted services")
            lines.append(", ".join(d["transitive_impacted_services"]))
        lines.append(f"\nTotal impacted nodes: {d['total_impacted_nodes']}")
        return "\n".join(lines)
    @staticmethod
    def _render_service_failure_impact_md(d: dict) -> str:
        lines = [f"# Failure impact for `{d['failed_service']['name']}`", ""]
        lines.append(f"Impacted services: {', '.join(d['impacted_services']) or 'none'}")
        lines.append(f"Total impacted nodes: {d['total_impacted_nodes']}")
        if d["impacted_endpoints"]:
            lines.append("\n## Impacted endpoints")
            for ep in d["impacted_endpoints"][:50]:
                lines.append(f"- `{ep['name']}` (service={ep['service']})")
        return "\n".join(lines)
