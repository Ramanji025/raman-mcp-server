"""OpenAPI / Swagger spec parser: endpoint discovery from published contracts."""
from __future__ import annotations

import json

import yaml

from ...models import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    ParseResult,
    SourceFile,
)
from .base import Parser, make_node_id

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


class OpenApiParser(Parser):
    """Parses OpenAPI/Swagger spec files into Endpoint/DTO graph nodes."""

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse an OpenAPI/Swagger spec file into Endpoint/DTO graph nodes."""
        result = ParseResult()
        service = self._service_name(source)
        text = self._read(source)
        spec = self._load(text, source.rel_path)
        if not isinstance(spec, dict):
            return result

        title = (spec.get("info") or {}).get("title", service)
        svc_id = make_node_id("service", service, service)

        for path, item in (spec.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method, op in item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                    continue
                http = method.upper()
                ep_name = f"{http} {path}"
                ep_id = make_node_id("endpoint", service, ep_name)
                result.nodes.append(
                    GraphNode(
                        id=ep_id, type=NodeType.ENDPOINT, name=ep_name, service=service,
                        attributes={
                            "http_method": http, "path": path,
                            "operationId": op.get("operationId"),
                            "summary": op.get("summary"),
                            "tags": op.get("tags", []),
                            "source": "openapi", "file": source.rel_path,
                        },
                    )
                )
                result.edges.append(
                    GraphEdge(src=ep_id, dst=svc_id, type=EdgeType.DECLARED_IN)
                )

        # Schemas -> DTO nodes.
        schemas = ((spec.get("components") or {}).get("schemas")) or {}
        for name in schemas:
            result.nodes.append(
                GraphNode(id=make_node_id("dto", service, name), type=NodeType.DTO,
                          name=name, service=service, attributes={"source": "openapi"})
            )

        result.chunks.extend(
            self._chunk_text(source, text, collection="docs",
                             metadata={"artifact": "openapi", "title": title,
                                       "service": service})
        )
        return result

    @staticmethod
    def _load(text: str, rel_path: str):
        try:
            if rel_path.endswith(".json"):
                return json.loads(text)
            return yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError):
            return None
