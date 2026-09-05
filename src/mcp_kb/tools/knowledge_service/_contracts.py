"""KnowledgeService mixin: ContractsMixin (contracts domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

import json
import re

from ...logging import get_logger
from ...models import NodeType, ToolResponse

log = get_logger(__name__)




class ContractsMixin:
    """get_api_contract / get_execution_flow / generate_openapi tools."""
    def get_api_contract(self, endpoint_path: str) -> ToolResponse:
        """Return a complete API contract: auth, request/response JSON schemas, validation."""
        matches = self.graph.find_nodes(endpoint_path, {NodeType.ENDPOINT}, limit=10)
        if not matches:
            return self._not_found("get_api_contract", {"endpoint_path": endpoint_path},
                                   endpoint_path)
        contracts = []
        for ep in matches:
            attrs = ep.get("attributes", {})
            contract = self._build_full_contract(ep, attrs)
            contracts.append(contract)
        data = {"contracts": contracts, "count": len(contracts)}
        md = self._render_api_contract_md(endpoint_path, contracts)
        return ToolResponse(
            tool="get_api_contract", query={"endpoint_path": endpoint_path},
            summary=f"Full API contract for {len(contracts)} endpoint(s) matching '{endpoint_path}'.",
            data=data, markdown=md,
        )
    def get_execution_flow(self, endpoint_path: str) -> ToolResponse:
        """Return execution/business/technical flow knowledge for an endpoint path."""
        endpoints = self.graph.find_nodes(endpoint_path, {NodeType.ENDPOINT}, limit=10)
        if not endpoints:
            return self._not_found("get_execution_flow", {"endpoint_path": endpoint_path}, endpoint_path)

        flow_items = []
        for ep in endpoints:
            ep_id = ep["id"]
            flows = []
            for flow_type in (NodeType.EXECUTION_FLOW.value, NodeType.BUSINESS_FLOW.value, NodeType.TECHNICAL_FLOW.value):
                nodes = self.graph.nodes_by_type(NodeType(flow_type), ep.get("service"))
                for node in nodes:
                    attrs = node.get("attributes", {})
                    if attrs.get("endpoint_id") == ep_id:
                        flows.append({
                            "flow_type": node.get("type"),
                            "name": node.get("name"),
                            "summary": attrs.get("flow_summary", ""),
                            "entry_points": attrs.get("entry_points", []),
                            "exit_points": attrs.get("exit_points", []),
                            "side_effects": attrs.get("side_effects", []),
                            "downstream_impacts": attrs.get("downstream_impacts", []),
                            "execution_path": attrs.get("execution_path", []),
                        })
            flow_items.append({
                "endpoint": ep.get("name"),
                "service": ep.get("service"),
                "path": ep.get("attributes", {}).get("path"),
                "flows": flows,
            })

        md = self._render_execution_flow_md(endpoint_path, flow_items)
        return ToolResponse(
            tool="get_execution_flow",
            query={"endpoint_path": endpoint_path},
            summary=f"Retrieved execution-flow knowledge for {len(flow_items)} endpoint(s).",
            data={"flows": flow_items, "count": len(flow_items)},
            markdown=md,
        )
    def _build_full_contract(self, ep_node: dict, attrs: dict) -> dict:
        """Assemble a complete API contract from graph + DTO nodes."""
        # Resolve request DTO.
        req_dto = self._resolve_dto(attrs.get("request_body"), ep_node.get("service"))
        # Resolve response DTO.
        ret_type = attrs.get("returns", "void")
        resp_dto_name = re.sub(r".*<|>.*", "", ret_type).strip() \
            if "<" in ret_type else ret_type
        resp_dto_name = re.sub(r"ResponseEntity|List|Optional", "", resp_dto_name).strip("<>")
        resp_dto = self._resolve_dto(resp_dto_name or None, ep_node.get("service"))

        security = attrs.get("security")
        auth_info = {"required": bool(security), "expression": security or "none",
                     "note": ("Spring Security @PreAuthorize expression" if security else
                              "No method-level security annotation detected")}

        return {
            "endpoint": ep_node.get("name"),
            "service": ep_node.get("service"),
            "http_method": attrs.get("http_method"),
            "path": attrs.get("path"),
            "controller": attrs.get("controller"),
            "handler_method": attrs.get("handler"),
            "file": attrs.get("file"),
            "authentication": auth_info,
            "path_variables": attrs.get("path_variables", []),
            "query_parameters": attrs.get("query_params", []),
            "request_body": {
                "dto_name": attrs.get("request_body"),
                "fields": req_dto.get("attributes", {}).get("fields", []) if req_dto else [],
                "json_example": req_dto.get("attributes", {}).get("json_example", {}) if req_dto else {},
            },
            "response_body": {
                "type": ret_type,
                "dto_name": resp_dto_name if resp_dto else None,
                "fields": resp_dto.get("attributes", {}).get("fields", []) if resp_dto else [],
                "json_example": resp_dto.get("attributes", {}).get("json_example", {}) if resp_dto else {},
            },
            "error_responses": [{"exception": e, "likely_status": self._exception_to_status(e)}
                                 for e in attrs.get("thrown_exceptions", [])],
            "transactional": attrs.get("transactional", False),
            "invocation_chain": self._build_invocation_chain(ep_node),
        }
    def _resolve_dto(self, name: str | None, service: str | None) -> dict | None:
        if not name:
            return None
        candidates = self.graph.find_nodes(name, {NodeType.DTO}, limit=5)
        if service:
            for c in candidates:
                if c.get("service") == service:
                    return c
        return candidates[0] if candidates else None
    @staticmethod
    def _exception_to_status(exc: str) -> str:
        mapping = {
            "NotFoundException": "404", "ResourceNotFoundException": "404",
            "BadRequestException": "400", "ValidationException": "400",
            "UnauthorizedException": "401", "ForbiddenException": "403",
            "ConflictException": "409", "InternalServerErrorException": "500",
        }
        for k, v in mapping.items():
            if k.lower() in exc.lower():
                return v
        return "500"
    @staticmethod
    def _render_api_contract_md(query: str, contracts: list[dict]) -> str:
        lines = [f"# API Contract: `{query}`", ""]
        for c in contracts:
            auth = c.get("authentication", {})
            lines += [
                f"## {c.get('http_method', 'HTTP')} `{c.get('path', '?')}`",
                f"> Handler: `{c.get('controller')}.{c.get('handler_method')}`",
                f"> File: `{c.get('file')}`", "",
                "### Authentication & Authorization",
                f"- Required: {auth.get('required')}",
                f"- Expression: `{auth.get('expression')}`",
                f"- Note: {auth.get('note')}", "",
            ]
            pvars = c.get("path_variables", [])
            if pvars:
                lines.append("### Path Variables")
                for pv in pvars:
                    lines.append(f"- `{pv}`")
                lines.append("")
            qparams = c.get("query_parameters", [])
            if qparams:
                lines.append("### Query Parameters")
                for qp in qparams:
                    lines.append(f"- `{qp}`")
                lines.append("")
            rb = c.get("request_body", {})
            if rb.get("dto_name"):
                lines += [f"### Request Body — `{rb['dto_name']}`",
                          "```json",
                          json.dumps(rb.get("json_example", {}), indent=2),
                          "```", ""]
            resp = c.get("response_body", {})
            if resp.get("type"):
                lines += [f"### Response — `{resp['type']}`",
                          "```json",
                          json.dumps(resp.get("json_example", {}), indent=2),
                          "```", ""]
            errs = c.get("error_responses", [])
            if errs:
                lines.append("### Error Responses")
                for err in errs:
                    lines.append(f"- HTTP {err['likely_status']}: `{err['exception']}`")
                lines.append("")
            chain = c.get("invocation_chain", [])
            if chain:
                lines.append("### Invocation Chain")
                for step in chain:
                    indent = "  " * step.get("level", 0)
                    lines.append(f"{indent}- [{step.get('type')}] `{step.get('name')}`")
                lines.append("")
        return "\n".join(lines)
    def generate_openapi(self, service_name: str) -> ToolResponse:
        """Generate an OpenAPI 3.0 specification from the knowledge graph."""
        import yaml as pyyaml
        svc = self._resolve_service(service_name)
        endpoints = self.graph.nodes_by_type(NodeType.ENDPOINT, svc)
        svc_nodes = self.graph.find_nodes(svc, {NodeType.SERVICE}, limit=5)
        svc_node = next((n for n in svc_nodes if n.get("service") == svc), None)
        svc_attrs = svc_node.get("attributes", {}) if svc_node else {}

        dtos = self.graph.nodes_by_type(NodeType.DTO, svc)
        schemas: dict = {}
        for dto in dtos:
            schema_props = {}
            for field in dto.get("attributes", {}).get("fields", []):
                if isinstance(field, dict):
                    schema_props[field["name"]] = {
                        "type": self._java_type_to_openapi(field.get("type", "String")),
                        "nullable": field.get("nullable", True),
                    }
                else:
                    schema_props[str(field)] = {"type": "string"}
            schemas[dto.get("name", "UnknownDto")] = {
                "type": "object", "properties": schema_props,
            }

        paths: dict = {}
        for ep in endpoints:
            attrs = ep.get("attributes", {})
            path  = attrs.get("path", "/unknown")
            method = (attrs.get("http_method") or "get").lower()
            security = attrs.get("security")
            req_body_dto = attrs.get("request_body")
            ret_type = attrs.get("returns", "void")
            parameters = [{"name": pv, "in": "path", "required": True,
                           "schema": {"type": "string"}}
                          for pv in attrs.get("path_variables", [])]
            for qp in attrs.get("query_params", []):
                parameters.append({"name": qp, "in": "query", "required": False,
                                    "schema": {"type": "string"}})
            operation: dict = {
                "summary": ep.get("name", ""),
                "operationId": attrs.get("handler", ep.get("name", "")),
                "tags": [attrs.get("controller", svc)],
                "parameters": parameters,
                "responses": {
                    "200": {"description": "Success",
                            "content": {"application/json": {
                                "schema": {"$ref": f"#/components/schemas/{ret_type}"}
                                if ret_type and ret_type != "void" else {}}}},
                    **{err["likely_status"]: {"description": err["exception"]}
                       for err in [{"likely_status": self._exception_to_status(e),
                                    "exception": e}
                                   for e in attrs.get("thrown_exceptions", [])]},
                },
            }
            if security:
                operation["security"] = [{"bearerAuth": []}]
            if req_body_dto:
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {"$ref": f"#/components/schemas/{req_body_dto}"},
                    }},
                }
            paths.setdefault(path, {})[method] = operation

        spec: dict = {
            "openapi": "3.0.3",
            "info": {
                "title": svc,
                "version": svc_attrs.get("version", "1.0.0"),
                "description": f"Auto-generated from knowledge graph for {svc}",
            },
            "servers": [{"url": f"http://localhost:{svc_attrs.get('server_port', 8080)}"}],
            "components": {
                "schemas": schemas,
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
                },
            },
            "paths": paths,
        }
        spec_yaml = pyyaml.dump(spec, default_flow_style=False, allow_unicode=True)
        data = {"service": svc, "spec": spec, "spec_yaml": spec_yaml,
                "endpoint_count": len(endpoints), "schema_count": len(schemas)}
        md = f"# OpenAPI 3.0 Spec — `{svc}`\n\n```yaml\n{spec_yaml}\n```"
        return ToolResponse(
            tool="generate_openapi", query={"service_name": service_name},
            summary=f"OpenAPI 3.0 spec for {svc}: {len(endpoints)} paths, {len(schemas)} schemas.",
            data=data, markdown=md,
        )
    @staticmethod
    def _java_type_to_openapi(java_type: str) -> str:
        mapping = {
            "String": "string", "Long": "integer", "Integer": "integer",
            "Int": "integer", "Double": "number", "Float": "number",
            "Boolean": "boolean", "LocalDate": "string", "LocalDateTime": "string",
            "ZonedDateTime": "string", "BigDecimal": "number", "UUID": "string",
        }
        base = re.sub(r"<.*>", "", java_type).strip()
        return mapping.get(base, "object")
