"""KnowledgeService mixin: CapabilitiesMixin (capabilities domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse

log = get_logger(__name__)




class CapabilitiesMixin:
    """explain_business_capability tool."""
    def explain_business_capability(self, query: str) -> ToolResponse:
        """Explain one or more business capabilities and mapped technical implementations."""
        capabilities = self.graph.nodes_by_type(NodeType.BUSINESS_CAPABILITY)
        if not capabilities:
            return self._not_found("explain_business_capability", {"query": query}, query)

        matched = self._match_capabilities(query, capabilities)
        if not matched:
            matched = capabilities[:6]

        results = []
        for cap in matched:
            attrs = cap.get("attributes", {})
            nodes = self.graph.neighbors(cap["id"], direction="out")
            services = sorted({(n.get("node") or {}).get("name") for n in nodes
                               if (n.get("node") or {}).get("type") == NodeType.SERVICE.value})
            apis = sorted({(n.get("node") or {}).get("name") for n in nodes
                           if (n.get("node") or {}).get("type") == NodeType.ENDPOINT.value})
            databases = sorted({(n.get("node") or {}).get("name") for n in nodes
                                if (n.get("node") or {}).get("type") in {NodeType.DATABASE.value, NodeType.TABLE.value}})
            events = sorted({(n.get("node") or {}).get("name") for n in nodes
                             if (n.get("node") or {}).get("type") in {NodeType.TOPIC.value, NodeType.QUEUE.value}})

            validation_points = []
            if "payment" in cap.get("name", "").lower():
                validation_points = self._payment_validation_points()

            results.append({
                "capability": cap.get("name"),
                "summary": attrs.get("summary", ""),
                "services": [s for s in services if s],
                "apis": [a for a in apis if a],
                "databases": [d for d in databases if d],
                "events": [e for e in events if e],
                "flow_summaries": attrs.get("flow_summaries", []),
                "entry_points": attrs.get("apis", []),
                "exit_points": attrs.get("events", []),
                "downstream_impacts": attrs.get("services", []),
                "validation_points": validation_points,
            })

        md = self._render_capability_md(query, results)
        return ToolResponse(
            tool="explain_business_capability",
            query={"query": query},
            summary=f"Capability discovery matched {len(results)} capability item(s).",
            data={"capabilities": results, "count": len(results)},
            markdown=md,
        )
    @staticmethod
    def _match_capabilities(query: str, capabilities: list[dict]) -> list[dict]:
        q = query.lower().strip()
        out = []
        for cap in capabilities:
            name = (cap.get("name") or "").lower()
            attrs = str(cap.get("attributes") or "").lower()
            if q in name or q in attrs or any(t in name for t in q.split() if len(t) > 3):
                out.append(cap)
        return out
    def _payment_validation_points(self) -> list[dict]:
        points = []
        methods = self.graph.nodes_by_type(NodeType.METHOD)
        for m in methods:
            name = (m.get("name") or "").lower()
            attrs = m.get("attributes", {})
            body = str(attrs.get("body", "")).lower()
            if "validate" in name and "payment" in (name + " " + body):
                points.append({
                    "service": m.get("service"),
                    "class": attrs.get("class"),
                    "method": m.get("name"),
                    "file": attrs.get("file"),
                })
        return points[:25]
    @staticmethod
    def _render_capability_md(query: str, items: list[dict]) -> str:
        lines = [f"# Business capability discovery: `{query}`", ""]
        for item in items:
            lines.append(f"## {item['capability']}")
            if item.get("summary"):
                lines.append(item["summary"])
            lines.append(f"- Services: {', '.join(item.get('services', [])) or 'none'}")
            lines.append(f"- APIs: {', '.join(item.get('apis', [])) or 'none'}")
            lines.append(f"- Databases: {', '.join(item.get('databases', [])) or 'none'}")
            lines.append(f"- Events: {', '.join(item.get('events', [])) or 'none'}")
            if item.get("validation_points"):
                lines.append("- Payment validation points:")
                for p in item["validation_points"][:10]:
                    lines.append(
                        f"  {p['service']} :: {p['class']}.{p['method']} ({p['file']})"
                    )
            if item.get("flow_summaries"):
                lines.append("- Flow summaries:")
                for s in item["flow_summaries"][:6]:
                    lines.append(f"  {s}")
            lines.append("")
        return "\n".join(lines)
