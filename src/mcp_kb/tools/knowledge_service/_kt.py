"""KnowledgeService mixin: KtMixin (kt domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

import json

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class KtMixin:
    """full_kt (comprehensive knowledge-transfer) tool."""
    def full_kt(self, service_name: str) -> ToolResponse:
        """Comprehensive knowledge transfer: patterns, APIs, flows, config, onboarding."""
        svc = self._resolve_service(service_name)

        # Gather all graph data.
        controllers  = self.graph.nodes_by_type(NodeType.CONTROLLER, svc)
        endpoints    = self.graph.nodes_by_type(NodeType.ENDPOINT, svc)
        entities     = self.graph.nodes_by_type(NodeType.ENTITY, svc)
        repos        = self.graph.nodes_by_type(NodeType.REPOSITORY, svc)
        tables       = self.graph.nodes_by_type(NodeType.TABLE, svc)
        dtos         = self.graph.nodes_by_type(NodeType.DTO, svc)
        svc_layers   = self.graph.nodes_by_type(NodeType.SERVICE_LAYER, svc)
        methods      = self.graph.nodes_by_type(NodeType.METHOD, svc)
        producers    = self.graph.nodes_by_type(NodeType.KAFKA_PRODUCER, svc)
        consumers    = self.graph.nodes_by_type(NodeType.KAFKA_CONSUMER, svc)
        configs      = self.graph.nodes_by_type(NodeType.CONFIG, svc)
        patterns     = self.graph.nodes_by_type(NodeType.DESIGN_PATTERN, svc)
        dep_map      = self.graph.service_dependency_map()

        # Build full API contracts for all endpoints.
        api_contracts = []
        for ep in endpoints[:30]:  # cap at 30 to avoid enormous payloads
            api_contracts.append(self._build_full_contract(ep, ep.get("attributes", {})))

        # Build service layer call graph summary.
        call_graph = []
        for sl in svc_layers:
            inbound = [nb["node"].get("name") for nb in
                       self.graph.neighbors(sl["id"], direction="in")
                       if nb.get("node", {}).get("type") in ("Controller", "Endpoint")]
            outbound = [nb["node"].get("name") for nb in
                        self.graph.neighbors(sl["id"], direction="out")
                        if nb.get("node", {}).get("type") == "Repository"]
            call_graph.append({
                "service_class": sl.get("name"),
                "called_by": inbound,
                "calls_repos": outbound,
                "transactional": sl.get("attributes", {}).get("transactional", False),
                "config_props": sl.get("attributes", {}).get("config_properties", []),
            })

        # Semantic retrieval for architecture and documentation context.
        retrieval = self.retriever.retrieve(
            f"complete overview of {svc} architecture design patterns configuration",
            collections=("docs", "architecture", "code"), service=svc, top_k=10,
        )

        data = {
            "service": svc,
            "summary": {
                "controllers": len(controllers), "endpoints": len(endpoints),
                "service_classes": len(svc_layers), "methods": len(methods),
                "entities": len(entities), "repositories": len(repos),
                "tables": len(tables), "dtos": len(dtos),
                "kafka_producers": len(producers), "kafka_consumers": len(consumers),
                "design_patterns": len(patterns),
            },
            "design_patterns": [{"pattern": p.get("name"),
                                  "detected_in": p.get("attributes", {}).get("detected_in")}
                                 for p in patterns],
            "api_contracts": api_contracts,
            "service_layer_call_graph": call_graph,
            "entities": [{"name": e.get("name"),
                           "table": next((t.get("name") for t in tables
                                          if e.get("name", "").lower() in
                                          t.get("name", "").lower()), None)}
                          for e in entities],
            "dtos": [{"name": d.get("name"),
                      "fields": d.get("attributes", {}).get("fields", []),
                      "json_example": d.get("attributes", {}).get("json_example", {})}
                     for d in dtos],
            "kafka_events": {
                "produces": [p.get("attributes", {}).get("topic") for p in producers],
                "consumes": [c.get("attributes", {}).get("topic") for c in consumers],
            },
            "configuration": [c.get("attributes", {}) for c in configs],
            "dependencies": {
                "depends_on": dep_map.get(svc, []),
                "consumed_by": [s for s, deps in dep_map.items() if svc in deps],
            },
        }
        md = self._render_full_kt_md(svc, data, retrieval)
        return ToolResponse(
            tool="full_kt", query={"service_name": service_name},
            summary=(f"Complete KT for {svc}: {len(endpoints)} APIs, "
                     f"{len(patterns)} design patterns, {len(svc_layers)} service classes."),
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    @staticmethod
    def _render_full_kt_md(svc: str, d: dict,
                           retrieval: HybridResult | None) -> str:
        s = d["summary"]
        lines = [
            f"# Knowledge Transfer — `{svc}`", "",
            "## Service at a Glance",
            "| Category | Count |", "|---|---|",
            f"| Controllers | {s['controllers']} |",
            f"| Endpoints | {s['endpoints']} |",
            f"| Service Classes | {s['service_classes']} |",
            f"| Methods | {s['methods']} |",
            f"| Entities | {s['entities']} |",
            f"| Repositories | {s['repositories']} |",
            f"| DTOs | {s['dtos']} |",
            f"| Kafka Producers | {s['kafka_producers']} |",
            f"| Kafka Consumers | {s['kafka_consumers']} |",
            f"| Design Patterns | {s['design_patterns']} |", "",
        ]
        if d["design_patterns"]:
            lines += ["## Design Patterns Detected"]
            for p in d["design_patterns"]:
                lines.append(f"- **{p['pattern']}** — detected in `{p['detected_in']}`")
            lines.append("")
        if d["api_contracts"]:
            lines += ["## APIs", f"*{len(d['api_contracts'])} endpoint(s)*", ""]
            for c in d["api_contracts"]:
                auth_expr = c.get("authentication", {}).get("expression", "none")
                lines.append(f"### `{c.get('http_method')} {c.get('path')}`")
                lines.append(f"- Auth: `{auth_expr}`")
                rb = c.get("request_body", {})
                if rb.get("dto_name"):
                    lines.append(f"- Request: `{rb['dto_name']}`")
                    lines.append(f"```json\n{json.dumps(rb.get('json_example', {}), indent=2)}\n```")
                resp = c.get("response_body", {})
                if resp.get("type"):
                    lines.append(f"- Response: `{resp['type']}`")
                errs = c.get("error_responses", [])
                if errs:
                    err_str = ", ".join(f"HTTP {e['likely_status']} ({e['exception']})"
                                        for e in errs)
                    lines.append(f"- Errors: {err_str}")
                chain = c.get("invocation_chain", [])
                if chain:
                    flow = " → ".join(step.get("name", "?") for step in chain)
                    lines.append(f"- Flow: `{flow}`")
                lines.append("")
        if d["service_layer_call_graph"]:
            lines += ["## Service Layer Call Graph"]
            for sl in d["service_layer_call_graph"]:
                lines.append(f"**`{sl['service_class']}`**")
                if sl["called_by"]:
                    lines.append(f"  - Called by: {', '.join(f'`{x}`' for x in sl['called_by'])}")
                if sl["calls_repos"]:
                    lines.append(f"  - Calls repos: {', '.join(f'`{x}`' for x in sl['calls_repos'])}")
                if sl["transactional"]:
                    lines.append("  - @Transactional: Yes")
                if sl["config_props"]:
                    lines.append(f"  - Config: {sl['config_props']}")
            lines.append("")
        if d.get("kafka_events"):
            evs = d["kafka_events"]
            if evs.get("produces") or evs.get("consumes"):
                lines += ["## Kafka Events"]
                if evs.get("produces"):
                    lines.append(f"- Produces: {', '.join(f'`{t}`' for t in evs['produces'] if t)}")
                if evs.get("consumes"):
                    lines.append(f"- Consumes: {', '.join(f'`{t}`' for t in evs['consumes'] if t)}")
                lines.append("")
        if d["entities"]:
            lines += ["## Entities & Tables"]
            for e in d["entities"]:
                tbl = f" → table `{e['table']}`" if e.get("table") else ""
                lines.append(f"- `{e['name']}`{tbl}")
            lines.append("")
        if d["dtos"]:
            lines += ["## DTOs"]
            for dto in d["dtos"]:
                fields_info = ", ".join(
                    f["name"] if isinstance(f, dict) else str(f)
                    for f in dto.get("fields", [])[:10]
                )
                lines.append(f"- **`{dto['name']}`**: {fields_info or '(no fields extracted)'}")
                if dto.get("json_example"):
                    lines.append(f"  ```json\n  {json.dumps(dto['json_example'], indent=2)}\n  ```")
            lines.append("")
        deps = d.get("dependencies", {})
        if deps.get("depends_on") or deps.get("consumed_by"):
            lines += ["## Dependencies"]
            if deps["depends_on"]:
                lines.append(f"- Calls: {', '.join(f'`{s}`' for s in deps['depends_on'])}")
            if deps["consumed_by"]:
                lines.append(f"- Used by: {', '.join(f'`{s}`' for s in deps['consumed_by'])}")
            lines.append("")
        if retrieval and retrieval.chunks:
            lines += ["## Architecture & Documentation Context"]
            for rc in retrieval.chunks[:6]:
                lines.append(f"> `{rc.chunk.rel_path}` — {rc.chunk.text[:300]}")
        lines += ["", "---",
                  "*Generated by mcp-microservices-kb `full_kt` tool.*",
                  "*Use `get_api_contract` for deep per-endpoint contracts.*",
                  "*Use `compare_branches` for change impact.*"]
        return "\n".join(lines)
