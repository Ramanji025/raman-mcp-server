"""KnowledgeService mixin: ServiceOverviewMixin (service_overview domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import CallerSummary, NodeType, ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class ServiceOverviewMixin:
    """explain_service / find_endpoint / trace_business_flow / impact_analysis tools."""
    def explain_service(self, service_name: str) -> ToolResponse:
        """Overview of one microservice: endpoints, entities, tables, Kafka events, dependencies."""
        svc = self._resolve_service(service_name)
        controllers = self.graph.nodes_by_type(NodeType.CONTROLLER, svc)
        endpoints = self.graph.nodes_by_type(NodeType.ENDPOINT, svc)
        entities = self.graph.nodes_by_type(NodeType.ENTITY, svc)
        repos = self.graph.nodes_by_type(NodeType.REPOSITORY, svc)
        tables = self.graph.nodes_by_type(NodeType.TABLE, svc)
        producers = self.graph.nodes_by_type(NodeType.KAFKA_PRODUCER, svc)
        consumers = self.graph.nodes_by_type(NodeType.KAFKA_CONSUMER, svc)
        dep_map = self.graph.service_dependency_map()
        depends_on = dep_map.get(svc, [])
        dependents = sorted([s for s, ds in dep_map.items() if svc in ds])

        retrieval = self.retriever.retrieve(
            f"overview of {svc} responsibilities and architecture",
            collections=("docs", "architecture", "code"), service=svc,
            boosts=self.learner.retrieval_boosts(),
        )

        data = {
            "service": svc,
            "controllers": [n["name"] for n in controllers],
            "endpoint_count": len(endpoints),
            "endpoints": [n["attributes"] for n in endpoints[:50]],
            "entities": [n["name"] for n in entities],
            "repositories": [n["name"] for n in repos],
            "tables": [n["name"] for n in tables],
            "kafka_producers": [n["attributes"] for n in producers],
            "kafka_consumers": [n["attributes"] for n in consumers],
            "depends_on": depends_on,
            "consumed_by": dependents,
        }
        md = self._render_service_md(svc, data)
        return ToolResponse(
            tool="explain_service", query={"service_name": service_name},
            summary=(f"{svc}: {len(endpoints)} endpoints, {len(entities)} entities, "
                     f"{len(tables)} tables, depends on {len(depends_on)} services."),
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    def find_endpoint(self, api_name: str) -> ToolResponse:
        """Find REST endpoint(s) matching a name/path, with their full invocation chain."""
        matches = self.graph.find_nodes(api_name, {NodeType.ENDPOINT}, limit=25)
        retrieval = self.retriever.retrieve(
            api_name, collections=("code", "docs"), kind="endpoint", top_k=8,
            boosts=self.learner.retrieval_boosts(),
        )
        endpoints = []
        for m in matches:
            attrs = m.get("attributes", {})
            chain = self._build_invocation_chain(m)
            endpoints.append({
                "endpoint": m["name"],
                "service": m.get("service"),
                "http_method": attrs.get("http_method"),
                "path": attrs.get("path"),
                "handler": attrs.get("handler"),
                "controller": attrs.get("controller"),
                "returns": attrs.get("returns"),
                "file": attrs.get("file"),
                "request_body": attrs.get("request_body"),
                "path_variables": attrs.get("path_variables", []),
                "query_params": attrs.get("query_params", []),
                "thrown_exceptions": attrs.get("thrown_exceptions", []),
                "transactional": attrs.get("transactional", False),
                "security": attrs.get("security"),
                "invocation_chain": chain,
            })
        data = {"matches": endpoints, "match_count": len(endpoints)}
        md = self._render_endpoints_md(api_name, endpoints, retrieval)
        return ToolResponse(
            tool="find_endpoint", query={"api_name": api_name},
            summary=f"Found {len(endpoints)} endpoint(s) matching '{api_name}'.",
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    def _build_invocation_chain(self, ep_node: dict) -> list[dict]:
        """Walk DELEGATES_TO edges from endpoint → service layer → repository."""
        chain: list[dict] = []
        attrs = ep_node.get("attributes", {})
        # Layer 1: controller method info.
        chain.append({
            "layer": "controller",
            "class": attrs.get("controller", "?"),
            "method": attrs.get("handler", "?"),
            "file": attrs.get("file", ""),
        })
        # Layer 2: service layer classes this endpoint delegates to.
        svc_neighbors = self.graph.neighbors(ep_node["id"], direction="out")
        for nb in svc_neighbors:
            n = nb.get("node") or {}
            if n.get("type") not in ("ServiceLayer", "Method"):
                continue
            svc_attrs = n.get("attributes", {})
            entry = {
                "layer": "service",
                "class": n.get("name", "?"),
                "transactional": svc_attrs.get("transactional", False),
                "thrown_exceptions": svc_attrs.get("thrown_exceptions", []),
            }
            # Layer 3: repositories this service method delegates to.
            repo_neighbors = self.graph.neighbors(n["id"], direction="out")
            repos = []
            for rnb in repo_neighbors:
                rn = rnb.get("node") or {}
                if rn.get("type") != "Repository":
                    continue
                rn_attrs = rn.get("attributes", {})
                repos.append({
                    "layer": "repository",
                    "class": rn.get("name", "?"),
                    "query_methods": rn_attrs.get("query_methods", []),
                })
                # Layer 4: entities / tables.
                tbl_neighbors = self.graph.neighbors(rn["id"], direction="out")
                for tnb in tbl_neighbors:
                    tn = tnb.get("node") or {}
                    if tn.get("type") in ("Entity", "Table"):
                        repos[-1].setdefault("touches", []).append(
                            {"type": tn.get("type"), "name": tn.get("name")}
                        )
            entry["repositories"] = repos
            chain.append(entry)
        return chain
    def trace_business_flow(self, flow_name: str) -> ToolResponse:
        """Trace a named business flow end-to-end across participating services."""
        state = self.flows.trace_flow(flow_name)
        data = state.get("data", {})
        return ToolResponse(
            tool="trace_business_flow", query={"flow_name": flow_name},
            summary=(f"Flow '{flow_name}' spans "
                     f"{len(data.get('participating_services', []))} services."),
            data=data, markdown=state.get("answer", ""),
            citations=state.get("citations", []),
        )
    def impact_analysis(self, entity_or_table: str) -> ToolResponse:
        """Blast-radius analysis: what breaks if this entity/table changes."""
        node = self._resolve_entity_or_table(entity_or_table)
        if node is None:
            return self._not_found("impact_analysis",
                                   {"entity_or_table": entity_or_table},
                                   entity_or_table)
        impacted = self.graph.impacted(node["id"], max_hops=3)
        related = self.graph.neighbors(node["id"], direction="in") + \
            self.graph.neighbors(node["id"], direction="out")

        # Code-level blast radius: find endpoints and service methods that touch this node.
        callers = self._find_callers(node["id"])

        data = {
            "target": {"id": node["id"], "type": node.get("type"),
                       "name": node.get("name"), "service": node.get("service")},
            "impacted_services": impacted["services"],
            "total_impacted_nodes": impacted["total_impacted"],
            "levels": impacted["levels"],
            "direct_relations": [
                {"edge": r["edge_type"], "node": r["node"].get("name") if r["node"] else None,
                 "type": r["node"].get("type") if r["node"] else None}
                for r in related[:40]
            ],
            "calling_endpoints": callers["endpoints"],
            "calling_service_methods": callers["service_methods"],
            "calling_repositories": callers["repositories"],
            "change_risk_summary": (
                f"Changing '{node.get('name')}' will directly affect "
                f"{len(callers['endpoints'])} endpoint(s), "
                f"{len(callers['service_methods'])} service method(s), and "
                f"{len(callers['repositories'])} repository(-ies). "
                f"Total graph blast radius: {impacted['total_impacted']} nodes "
                f"across {len(impacted['services'])} service(s)."
            ),
        }
        md = self._render_impact_md(entity_or_table, data)
        return ToolResponse(
            tool="impact_analysis", query={"entity_or_table": entity_or_table},
            summary=data["change_risk_summary"],
            data=data, markdown=md,
        )
    def _find_callers(self, node_id: str) -> CallerSummary:
        """Traverse graph inward to find all endpoints/methods/repos that reach node_id."""
        endpoints, service_methods, repositories = [], [], []
        visited: set[str] = set()
        queue = [node_id]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            inbound = self.graph.neighbors(current, direction="in")
            for nb in inbound:
                n = nb.get("node") or {}
                ntype = n.get("type", "")
                nid = n.get("id", "")
                if ntype == "Endpoint":
                    ep = {"name": n.get("name"), "service": n.get("service"),
                          **{k: n.get("attributes", {}).get(k)
                             for k in ("path", "http_method", "controller", "handler")}}
                    if ep not in endpoints:
                        endpoints.append(ep)
                elif ntype == "Method":
                    sm = {"name": n.get("name"), "service": n.get("service"),
                          "class": n.get("attributes", {}).get("class")}
                    if sm not in service_methods:
                        service_methods.append(sm)
                elif ntype == "Repository":
                    repo = {"name": n.get("name"), "service": n.get("service")}
                    if repo not in repositories:
                        repositories.append(repo)
                if nid and nid not in visited:
                    queue.append(nid)
        return {"endpoints": endpoints, "service_methods": service_methods,
                "repositories": repositories}
    @staticmethod
    def _render_service_md(svc: str, d: dict) -> str:
        lines = [f"# Service: `{svc}`", ""]
        lines.append(f"- **Endpoints:** {d['endpoint_count']}")
        lines.append(f"- **Entities:** {', '.join(d['entities']) or '—'}")
        lines.append(f"- **Tables:** {', '.join(d['tables']) or '—'}")
        lines.append(f"- **Depends on:** {', '.join(d['depends_on']) or '—'}")
        lines.append(f"- **Consumed by:** {', '.join(d['consumed_by']) or '—'}")
        if d["endpoints"]:
            lines.append("\n## Endpoints")
            for e in d["endpoints"][:25]:
                lines.append(f"- `{e.get('http_method')} {e.get('path')}` "
                             f"→ {e.get('controller')}.{e.get('handler')}")
        if d["kafka_producers"] or d["kafka_consumers"]:
            lines.append("\n## Events")
            for p in d["kafka_producers"]:
                lines.append(f"- produces → `{p.get('topic')}`")
            for c in d["kafka_consumers"]:
                lines.append(f"- consumes ← `{c.get('topic')}`")
        return "\n".join(lines)
    @staticmethod
    def _render_endpoints_md(api: str, endpoints: list[dict], r: HybridResult) -> str:
        lines = [f"# Endpoint search: `{api}`", ""]
        if not endpoints:
            lines.append("_No structural matches; showing semantic results below._")
        for e in endpoints:
            lines.append(f"- `{e.get('http_method')} {e.get('path')}` "
                         f"in **{e.get('service')}** "
                         f"({e.get('controller')}.{e.get('handler')}) → {e.get('file')}")
        if r.chunks:
            lines.append("\n## Semantic matches")
            for rc in r.chunks[:5]:
                lines.append(f"- `{rc.chunk.repo}/{rc.chunk.rel_path}`")
        return "\n".join(lines)
    @staticmethod
    def _render_impact_md(term: str, d: dict) -> str:
        t = d["target"]
        lines = [f"# Impact analysis: `{t['name']}` ({t['type']})", "",
                 f"Owning service: **{t['service']}**", "",
                 f"**{d['total_impacted_nodes']} nodes** across "
                 f"**{len(d['impacted_services'])} services** are affected."]
        if d["impacted_services"]:
            lines.append(f"\n**Impacted services:** {', '.join(d['impacted_services'])}")
        for i, level in enumerate(d["levels"], start=1):
            lines.append(f"\n### Hop {i}")
            for n in level[:20]:
                lines.append(f"- {n.get('type')}: `{n.get('name')}` "
                             f"(via {n.get('via')}, service={n.get('service')})")
        return "\n".join(lines)
    @staticmethod
    def _render_execution_flow_md(endpoint_path: str, items: list[dict]) -> str:
        lines = [f"# Execution flows for `{endpoint_path}`", ""]
        for item in items:
            lines.append(f"## {item['endpoint']} ({item['service']})")
            for flow in item.get("flows", []):
                lines.append(f"- **{flow['flow_type']}**: {flow['summary']}")
                if flow.get("execution_path"):
                    lines.append(f"  path: {' -> '.join(flow['execution_path'])}")
                if flow.get("entry_points"):
                    lines.append(f"  entry: {', '.join(flow['entry_points'])}")
                if flow.get("exit_points"):
                    lines.append(f"  exit: {', '.join(flow['exit_points'])}")
                if flow.get("side_effects"):
                    lines.append(f"  side effects: {', '.join(flow['side_effects'])}")
                if flow.get("downstream_impacts"):
                    lines.append(f"  downstream impact: {', '.join(flow['downstream_impacts'])}")
            lines.append("")
        return "\n".join(lines)
