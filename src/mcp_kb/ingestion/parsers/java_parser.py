"""Tree-sitter based Java parser.

Extracts the Spring Boot code knowledge graph (goal #5): controllers,
endpoints, DTOs, entities, repositories, tables, Kafka producers/consumers
and inter-service calls, plus class/method level chunks for vector search.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import tree_sitter_java as tsjava
from tree_sitter import Language, Node
from tree_sitter import Parser as TSParser

from ...models import (
    Chunk,
    ContentType,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    ParseResult,
    SourceFile,
)
from .base import Parser, make_chunk_id, make_node_id

_JAVA_LANGUAGE = Language(tsjava.language())

_HTTP_MAPPING = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_CONTROLLER_ANN = {"RestController", "Controller"}
_SERVICE_ANN = {"Service", "Component", "ServiceImpl"}
_REPO_SUPERTYPES = {
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository",
    "MongoRepository", "ReactiveCrudRepository",
}
_SECURITY_ANN = {"PreAuthorize", "PostAuthorize", "Secured", "RolesAllowed"}

# Design pattern annotation/supertype signatures.
_PATTERN_SIGNALS: list[tuple[str, str, str]] = [
    # (pattern_name, detection_type, signal)
    ("Repository Pattern",      "annotation",   "Repository"),
    ("AOP / Decorator",         "annotation",   "Aspect"),
    ("Circuit Breaker",         "annotation",   "CircuitBreaker"),
    ("Retry Pattern",           "annotation",   "Retry"),
    ("Cache-Aside",             "annotation",   "Cacheable"),
    ("Async Pattern",           "annotation",   "Async"),
    ("Scheduler Pattern",       "annotation",   "Scheduled"),
    ("Observer / Event",        "annotation",   "EventListener"),
    ("Builder Pattern",         "annotation",   "Builder"),
    ("Proxy / Feign Client",    "annotation",   "FeignClient"),
    ("Transactional / UoW",     "annotation",   "Transactional"),
    ("Factory Pattern",         "name_suffix",  "Factory"),
    ("Builder Pattern",         "name_suffix",  "Builder"),
    ("Strategy Pattern",        "kind",         "interface"),
    ("Template Method",         "kind",         "abstract"),
]

_VALIDATION_ANN = {
    "NotNull", "NotBlank", "NotEmpty", "Size", "Min", "Max",
    "Pattern", "Email", "Positive", "Negative", "Future", "Past",
    "AssertTrue", "AssertFalse", "DecimalMin", "DecimalMax", "Valid",
}

_JAVA_TYPE_EXAMPLES: dict[str, Any] = {
    "String": "string_value", "Long": 1, "Integer": 1, "Int": 1,
    "Double": 1.0, "Float": 1.0, "Boolean": True, "bool": True,
    "LocalDate": "2024-01-15", "LocalDateTime": "2024-01-15T10:30:00",
    "ZonedDateTime": "2024-01-15T10:30:00Z", "Instant": "2024-01-15T10:30:00Z",
    "BigDecimal": "99.99", "UUID": "550e8400-e29b-41d4-a716-446655440000",
    "byte[]": "base64encodedstring",
}


@dataclass(slots=True)
class _ClassInfo:
    name: str
    kind: str  # class | interface | enum | record
    annotations: dict[str, dict[str, Any]]
    supertypes: list[str]
    start_line: int
    end_line: int
    body: Node | None
    header: str  # declaration text up to the body (extends/implements clause)


class JavaParser(Parser):
    """Parses a single ``.java`` file into graph nodes/edges and chunks."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._parser = TSParser(_JAVA_LANGUAGE)

    def parse(self, source: SourceFile) -> ParseResult:
        """Parse one Java source file into Controller/Service/Entity/Repository graph nodes."""
        code = self._read(source)
        src = code.encode("utf-8")
        tree = self._parser.parse(src)
        root = tree.root_node
        result = ParseResult()
        service = self._service_name(source)

        package = self._package_name(root, src)
        classes = self._collect_classes(root, src)

        for info in classes:
            self._emit_class(source, service, package, info, src, result)

        # Whole-file fallback chunk keeps small helper files searchable.
        if not any(c.metadata.get("kind") == "class" for c in result.chunks):
            result.chunks.extend(
                self._chunk_text(
                    source, code, collection="code",
                    metadata={"language": "java", "package": package},
                )
            )
        return result

    # ------------------------------------------------------------------ #
    # Class-level emission
    # ------------------------------------------------------------------ #
    def _emit_class(
        self,
        source: SourceFile,
        service: str,
        package: str,
        info: _ClassInfo,
        src: bytes,
        result: ParseResult,
    ) -> None:
        fqcn = f"{package}.{info.name}" if package else info.name
        ann = info.annotations
        base_attrs = {"fqcn": fqcn, "package": package, "file": source.rel_path,
                      "kind": info.kind}

        is_controller = bool(_CONTROLLER_ANN & ann.keys())
        is_entity = "Entity" in ann
        is_repository = (
            "Repository" in ann
            or any(s in _REPO_SUPERTYPES for s in info.supertypes)
            or any(s in info.header for s in _REPO_SUPERTYPES)
        )
        is_service_layer = bool(_SERVICE_ANN & ann.keys()) and not is_controller
        is_dto = self._looks_like_dto(info)
        is_feign = "FeignClient" in ann

        # Collect field injections once; used for call-graph edges below.
        injections = self._extract_field_injections(info, src)
        config_props = self._extract_value_properties(info, src)

        # ---- Controller + endpoints ---- #
        if is_controller:
            ctrl_id = make_node_id("controller", service, info.name)
            result.nodes.append(
                GraphNode(id=ctrl_id, type=NodeType.CONTROLLER, name=info.name,
                          service=service,
                          attributes={**base_attrs, "injected_services": list(injections.values())})
            )
            self._emit_endpoints(source, service, info, ctrl_id, src, result, injections)
            self._emit_delegation_edges(service, ctrl_id, injections, result)
            self._emit_service_methods(source, service, info, ctrl_id, src, result, injections)

        # ---- Service layer ---- #
        if is_service_layer:
            svc_id = make_node_id("service_layer", service, info.name)
            transactional = "Transactional" in ann
            result.nodes.append(
                GraphNode(id=svc_id, type=NodeType.SERVICE_LAYER, name=info.name,
                          service=service,
                          attributes={**base_attrs,
                                      "transactional": transactional,
                                      "injected_repos": [v for v in injections.values()
                                                         if "Repository" in v or "Repo" in v],
                                      "config_properties": config_props})
            )
            result.edges.append(
                GraphEdge(src=make_node_id("service", service, service),
                          dst=svc_id, type=EdgeType.DECLARED_IN)
            )
            self._emit_delegation_edges(service, svc_id, injections, result)
            self._emit_service_methods(source, service, info, svc_id, src, result, injections)

        # ---- Entity + table ---- #
        if is_entity:
            ent_id = make_node_id("entity", service, info.name)
            columns, relationships, antipatterns = self._extract_entity_schema(info, src)
            result.nodes.append(
                GraphNode(id=ent_id, type=NodeType.ENTITY, name=info.name,
                          service=service,
                          attributes={**base_attrs, "columns": columns,
                                      "relationships": relationships,
                                      "antipatterns": antipatterns})
            )
            table = self._table_name(ann, info.name)
            table_id = make_node_id("table", service, table)
            result.nodes.append(
                GraphNode(id=table_id, type=NodeType.TABLE, name=table,
                          service=service, attributes={"entity": info.name,
                                                       "columns": columns})
            )
            result.edges.append(
                GraphEdge(src=ent_id, dst=table_id, type=EdgeType.MAPS_TO)
            )
            # JPA relationships → HAS_RELATIONSHIP edges.
            for rel in relationships:
                target_entity = rel.get("target_entity")
                if target_entity:
                    result.edges.append(
                        GraphEdge(src=ent_id,
                                  dst=make_node_id("entity", service, target_entity),
                                  type=EdgeType.HAS_RELATIONSHIP,
                                  attributes={"type": rel.get("type"),
                                              "field": rel.get("field"),
                                              "fetch": rel.get("fetch", "EAGER"),
                                              "cascade": rel.get("cascade", [])})
                    )

        # ---- Exception type ---- #
        is_exception = any(s in info.supertypes for s in
                           ("Exception", "RuntimeException", "Throwable",
                            "ApplicationException", "BaseException"))
        if is_exception:
            exc_id = make_node_id("exception", service, info.name)
            result.nodes.append(
                GraphNode(id=exc_id, type=NodeType.EXCEPTION_TYPE, name=info.name,
                          service=service,
                          attributes={**base_attrs,
                                      "extends": list(info.supertypes),
                                      "http_status": self._exception_http_status(info.name)})
            )
            for parent in info.supertypes:
                result.edges.append(
                    GraphEdge(src=exc_id,
                              dst=make_node_id("exception", service, parent),
                              type=EdgeType.EXTENDS,
                              attributes={"parent": parent})
                )

        # ---- Test class ---- #
        is_test = (any(a in ann for a in ("SpringBootTest", "ExtendWith", "RunWith",
                                          "WebMvcTest", "DataJpaTest", "MockitoExtension")) or
                   info.name.endswith(("Test", "Tests", "IT", "Spec")))
        if is_test:
            test_id = make_node_id("test", service, info.name)
            tested = self._infer_tested_class(info)
            result.nodes.append(
                GraphNode(id=test_id, type=NodeType.TEST_CLASS, name=info.name,
                          service=service,
                          attributes={**base_attrs,
                                      "tested_class": tested,
                                      "test_framework": self._test_framework(ann),
                                      "test_type": self._test_type(ann)})
            )
            if tested:
                result.edges.append(
                    GraphEdge(src=make_node_id("class", service, tested),
                              dst=test_id, type=EdgeType.TESTED_BY,
                              attributes={"test_class": info.name})
                )

        # ---- Repository ---- #
        if is_repository:
            repo_id = make_node_id("repository", service, info.name)
            query_methods = self._extract_repository_methods(info, src)
            result.nodes.append(
                GraphNode(id=repo_id, type=NodeType.REPOSITORY, name=info.name,
                          service=service,
                          attributes={**base_attrs, "query_methods": query_methods})
            )
            managed = self._repository_entity(info)
            if managed:
                result.edges.append(
                    GraphEdge(
                        src=repo_id,
                        dst=make_node_id("entity", service, managed),
                        type=EdgeType.QUERIES,
                    )
                )
            # Emit a chunk per custom query method for fine-grained retrieval.
            for qm in query_methods:
                result.chunks.append(Chunk(
                    id=make_chunk_id(source.repo, source.rel_path, 0,
                                     f"{info.name}.{qm['name']}"),
                    repo=source.repo, rel_path=source.rel_path,
                    content_type=ContentType.JAVA, collection="code",
                    text=(f"// Repository: {info.name} | manages: {managed or '?'}\n"
                          f"// Method: {qm['name']} | query: {qm.get('query','derived')}\n"
                          f"{qm['signature']}"),
                    metadata={"kind": "repository_method", "repository": info.name,
                              "method": qm["name"], "service": service,
                              "managed_entity": managed or ""},
                ))

        # ---- DTO ---- #
        if is_dto and not (is_controller or is_entity or is_repository):
            dto_id = make_node_id("dto", service, info.name)
            fields = self._extract_dto_fields_full(info, src)
            json_example = self._generate_json_example(fields)
            result.nodes.append(
                GraphNode(id=dto_id, type=NodeType.DTO, name=info.name,
                          service=service,
                          attributes={**base_attrs, "fields": fields,
                                      "json_example": json_example})
            )

        # ---- Design patterns ---- #
        patterns = self._detect_design_patterns(info)
        for pattern_name in patterns:
            p_id = make_node_id("pattern", service, pattern_name)
            if not any(n.id == p_id for n in result.nodes):
                result.nodes.append(
                    GraphNode(id=p_id, type=NodeType.DESIGN_PATTERN, name=pattern_name,
                              service=service, attributes={"detected_in": info.name})
                )
            cls_id = (make_node_id("controller", service, info.name) if is_controller else
                      make_node_id("service_layer", service, info.name) if is_service_layer else
                      make_node_id("repository", service, info.name) if is_repository else
                      make_node_id("dto", service, info.name))
            result.edges.append(
                GraphEdge(src=cls_id, dst=p_id, type=EdgeType.USES_PATTERN,
                          attributes={"class": info.name})
            )

        # ---- Antipatterns (code smell detection) ---- #
        smells = self._detect_antipatterns(info, src, is_controller, is_service_layer,
                                           is_entity, injections, config_props)
        if smells:
            # Attach to the primary class node.
            for n in result.nodes:
                if n.service == service and n.name == info.name:
                    n.attributes["antipatterns"] = smells
                    break

        # ---- Feign / inter-service call ---- #
        if is_feign:
            target = self._feign_target(ann)
            if target:
                result.edges.append(
                    GraphEdge(
                        src=make_node_id("service", service, service),
                        dst=make_node_id("service", target, target),
                        type=EdgeType.CALLS,
                        attributes={"via": "feign", "client": info.name},
                    )
                )
                # P3.2: link each Feign interface method to the target endpoint it resolves to
                if info.body is not None:
                    base_path = self._request_mapping_path(ann)
                    for method in self._iter_children(info.body, "method_declaration"):
                        m_ann = self._method_annotations(method, src)
                        http, sub_path = self._method_http(m_ann)
                        if http and sub_path is not None:
                            ep_path = self._join_path(base_path, sub_path)
                            ep_name = f"{http} {ep_path}"
                            m_name = self._child_field_text(method, "name", src) or "unknown"
                            result.edges.append(
                                GraphEdge(
                                    src=make_node_id("method", service, f"{info.name}.{m_name}"),
                                    dst=make_node_id("endpoint", target, ep_name),
                                    type=EdgeType.RESOLVES_TO,
                                    attributes={"via": "feign", "client": info.name,
                                                "target_service": target},
                                )
                            )

        # ---- Kafka ---- #
        self._emit_kafka(source, service, info, src, result)

        # ---- Fields, constructors, methods, params, annotations, logic ---- #
        owner = next(
            (
                n for n in reversed(result.nodes)
                if n.service == service and n.name == info.name
                and n.type in {
                    NodeType.CONTROLLER, NodeType.SERVICE_LAYER, NodeType.ENTITY,
                    NodeType.REPOSITORY, NodeType.DTO, NodeType.EXCEPTION_TYPE,
                    NodeType.TEST_CLASS, NodeType.CLASS, NodeType.INTERFACE,
                }
            ),
            None,
        )
        if owner is None:
            owner_id = make_node_id(
                "interface" if info.kind == "interface" else "class",
                service, info.name,
            )
            owner_type = NodeType.INTERFACE if info.kind == "interface" else NodeType.CLASS
            result.nodes.append(
                GraphNode(
                    id=owner_id, type=owner_type, name=info.name, service=service,
                    attributes={**base_attrs, "annotations": list(ann.keys())},
                )
            )
            owner_id_final = owner_id
        else:
            owner_id_final = owner.id
        self._emit_structural_members(
            source, service, package, info, src, result, owner_id_final,
        )

        # ---- Class chunk for vector search ---- #
        class_text = src[self._class_start_byte(info):self._class_end_byte(info)].decode(
            "utf-8", errors="replace"
        )
        stereotype = (
            "controller" if is_controller else
            "entity" if is_entity else
            "repository" if is_repository else
            "dto" if is_dto else "class"
        )
        result.chunks.append(
            Chunk(
                id=make_chunk_id(source.repo, source.rel_path, 0, info.name),
                repo=source.repo,
                rel_path=source.rel_path,
                content_type=ContentType.JAVA,
                collection="code",
                text=class_text[:8000],
                start_line=info.start_line,
                end_line=info.end_line,
                metadata={"kind": "class", "stereotype": stereotype,
                          "class": info.name, "fqcn": fqcn, "service": service},
            )
        )
        # P3.1: emit per-method chunks for large classes so no method is lost to truncation
        if len(class_text) > 8000 and info.body is not None:
            self._emit_method_chunks(source, service, info, fqcn, src, result)

    # ------------------------------------------------------------------ #
    # P3.1: method-boundary chunks for large classes
    # ------------------------------------------------------------------ #
    def _emit_method_chunks(
        self,
        source: SourceFile,
        service: str,
        info: _ClassInfo,
        fqcn: str,
        src: bytes,
        result: ParseResult,
    ) -> None:
        """One chunk per method for classes truncated by the 8 kB class limit."""
        existing_ids = {c.id for c in result.chunks}
        for method in self._iter_children(info.body, "method_declaration"):
            m_name = self._child_field_text(method, "name", src) or "unknown"
            m_body = src[method.start_byte:method.end_byte].decode("utf-8", "replace")
            chunk_id = make_chunk_id(
                source.repo, source.rel_path, method.start_point[0], f"{fqcn}.{m_name}"
            )
            if chunk_id in existing_ids:
                continue  # already emitted by _emit_service_methods
            result.chunks.append(Chunk(
                id=chunk_id,
                repo=source.repo,
                rel_path=source.rel_path,
                content_type=ContentType.JAVA,
                collection="code",
                text=f"// {fqcn}.{m_name}\n" + m_body[:6000],
                start_line=method.start_point[0] + 1,
                end_line=method.end_point[0] + 1,
                metadata={"kind": "method", "class": info.name,
                          "method": m_name, "service": service, "fqcn": fqcn},
            ))

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def _emit_endpoints(
        self,
        source: SourceFile,
        service: str,
        info: _ClassInfo,
        ctrl_id: str,
        src: bytes,
        result: ParseResult,
        injections: dict[str, str] | None = None,
    ) -> None:
        base_path = self._request_mapping_path(info.annotations)
        if info.body is None:
            return
        injections = injections or {}
        for method in self._iter_children(info.body, "method_declaration"):
            m_annotations = self._method_annotations(method, src)
            http, sub_path = self._method_http(m_annotations)
            if http is None:
                continue
            path = self._join_path(base_path, sub_path)
            m_name = self._child_field_text(method, "name", src) or "unknown"
            ep_name = f"{http} {path}"
            ep_id = make_node_id("endpoint", service, ep_name)
            ret_type = self._child_field_text(method, "type", src) or "void"
            params = self._param_types(method, src)

            # Deep endpoint metadata.
            request_body = self._extract_request_body(method, src)
            path_vars = self._extract_path_variables(method, src)
            query_params = self._extract_query_params(method, src)
            thrown_exceptions = self._extract_throws(method, src)
            is_transactional = "Transactional" in m_annotations
            security = next((m_annotations[a].get("value", "")
                             for a in _SECURITY_ANN if a in m_annotations), None)
            response_status = m_annotations.get("ResponseStatus", {}).get("value") or \
                              m_annotations.get("ResponseStatus", {}).get("code")
            # Which injected service fields are called in this method body.
            delegated_to = self._method_delegations(method, src, injections)

            result.nodes.append(
                GraphNode(
                    id=ep_id, type=NodeType.ENDPOINT, name=ep_name, service=service,
                    attributes={
                        "http_method": http, "path": path, "handler": m_name,
                        "controller": info.name, "returns": ret_type,
                        "params": params, "file": source.rel_path,
                        "request_body": request_body,
                        "path_variables": path_vars,
                        "query_params": query_params,
                        "thrown_exceptions": thrown_exceptions,
                        "transactional": is_transactional,
                        "security": security,
                        "response_status": response_status,
                        "delegates_to": delegated_to,
                    },
                )
            )
            result.edges.append(GraphEdge(src=ctrl_id, dst=ep_id, type=EdgeType.EXPOSES))
            result.edges.append(
                GraphEdge(src=ep_id, dst=make_node_id("service", service, service),
                          type=EdgeType.DECLARED_IN)
            )
            # Delegation edges: endpoint → service layer classes it calls.
            for svc_type in delegated_to:
                result.edges.append(
                    GraphEdge(src=ep_id,
                              dst=make_node_id("service_layer", service, svc_type),
                              type=EdgeType.DELEGATES_TO,
                              attributes={"via": "controller_method", "handler": m_name})
                )
            # Exception edges.
            for exc in thrown_exceptions:
                result.edges.append(
                    GraphEdge(src=ep_id,
                              dst=make_node_id("exception", service, exc),
                              type=EdgeType.THROWS)
                )
            # DTO relations.
            for dto in self._dto_type_names(ret_type):
                result.edges.append(
                    GraphEdge(src=ep_id, dst=make_node_id("dto", service, dto),
                              type=EdgeType.RETURNS)
                )
            if request_body:
                result.edges.append(
                    GraphEdge(src=ep_id, dst=make_node_id("dto", service, request_body),
                              type=EdgeType.ACCEPTS)
                )
            for p in params:
                for dto in self._dto_type_names(p):
                    result.edges.append(
                        GraphEdge(src=ep_id, dst=make_node_id("dto", service, dto),
                                  type=EdgeType.ACCEPTS)
                    )

            # Endpoint chunk — enriched with full metadata header.
            m_text = src[method.start_byte:method.end_byte].decode("utf-8", "replace")
            chain_header = self._build_chain_header(
                ep_name, info.name, m_name, request_body, path_vars,
                query_params, ret_type, thrown_exceptions,
                is_transactional, security, delegated_to,
            )
            result.chunks.append(
                Chunk(
                    id=make_chunk_id(source.repo, source.rel_path,
                                     method.start_point[0], ep_name),
                    repo=source.repo, rel_path=source.rel_path,
                    content_type=ContentType.JAVA, collection="code",
                    text=(chain_header + m_text)[:8000],
                    start_line=method.start_point[0] + 1,
                    end_line=method.end_point[0] + 1,
                    metadata={"kind": "endpoint", "http_method": http, "path": path,
                              "handler": m_name, "service": service,
                              "request_body": request_body or "",
                              "delegates_to": ",".join(delegated_to),
                              "thrown_exceptions": ",".join(thrown_exceptions)},
                )
            )

    # ------------------------------------------------------------------ #
    # Service layer methods
    # ------------------------------------------------------------------ #
    def _emit_service_methods(
        self, source: SourceFile, service: str, info: _ClassInfo,
        svc_id: str, src: bytes, result: ParseResult,
        injections: dict[str, str],
    ) -> None:
        """Emit METHOD nodes for every non-private method with full body + call graph."""
        if info.body is None:
            return
        for method in self._iter_children(info.body, "method_declaration"):
            m_ann = self._method_annotations(method, src)
            m_name = self._child_field_text(method, "name", src) or "unknown"
            mod_node = (method.child_by_field_name("modifiers") or
                        next((c for c in method.children if c.type == "modifiers"), None))
            modifiers_text = self._text(mod_node, src) if mod_node else ""
            if "private" in modifiers_text:
                continue

            transactional = "Transactional" in m_ann
            thrown        = self._extract_throws(method, src)
            delegated     = self._method_delegations(method, src, injections)
            ret_type      = self._child_field_text(method, "type", src) or "void"
            params        = self._extract_method_params(method, src)
            called        = self._extract_called_methods(method, src)
            local_vars    = self._extract_local_variables(method, src)
            m_body_text   = src[method.start_byte:method.end_byte].decode("utf-8", "replace")
            line_count    = m_body_text.count("\n") + 1

            method_id = make_node_id("method", service, f"{info.name}.{m_name}")
            result.nodes.append(
                GraphNode(id=method_id, type=NodeType.METHOD, name=m_name,
                          service=service,
                          attributes={
                              "class": info.name,
                              "fqcn": f"{info.name}.{m_name}",
                              "returns": ret_type,
                              "parameters": params,
                              "transactional": transactional,
                              "thrown_exceptions": thrown,
                              "delegates_to_repos": delegated,
                              "injected_fields": injections,
                              "called_methods": called,
                              "local_variables": local_vars,
                              "annotations": list(m_ann.keys()),
                              "line_count": line_count,
                              "start_line": method.start_point[0] + 1,
                              "end_line": method.end_point[0] + 1,
                              "body": m_body_text[:4000],  # cap for graph storage
                              "file": source.rel_path,
                          })
            )
            result.edges.append(
                GraphEdge(src=svc_id, dst=method_id, type=EdgeType.DECLARED_IN)
            )
            for repo_type in delegated:
                result.edges.append(
                    GraphEdge(src=method_id,
                              dst=make_node_id("repository", service, repo_type),
                              type=EdgeType.DELEGATES_TO,
                              attributes={"via": "service_method"})
                )
            # Rich chunk header enables "fix the X logic in methodName" prompts.
            param_str = ", ".join(f"{p['type']} {p['name']}" for p in params)
            header = (
                f"// ═══ METHOD: {info.name}.{m_name}({param_str}) → {ret_type} ═══\n"
                f"// Class     : {info.name} | Service: {service}\n"
                f"// File      : {source.rel_path} | Lines: {method.start_point[0]+1}-{method.end_point[0]+1}\n"
                f"// Transactional: {transactional} | Throws: {thrown}\n"
                f"// Calls     : {called[:10]}\n"
                f"// LocalVars : {[v['name'] for v in local_vars[:8]]}\n"
            )
            result.chunks.append(Chunk(
                id=make_chunk_id(source.repo, source.rel_path,
                                 method.start_point[0], f"{info.name}.{m_name}"),
                repo=source.repo, rel_path=source.rel_path,
                content_type=ContentType.JAVA, collection="code",
                text=(header + m_body_text)[:6000],
                start_line=method.start_point[0] + 1,
                end_line=method.end_point[0] + 1,
                metadata={"kind": "method", "class": info.name,
                          "method": m_name, "service": service,
                          "transactional": str(transactional),
                          "called_methods": ",".join(called[:10])},
            ))

    _LOGIC_BLOCK_TYPES = {
        "if_statement": "if",
        "for_statement": "for",
        "enhanced_for_statement": "for-each",
        "while_statement": "while",
        "do_statement": "do-while",
        "try_statement": "try",
        "catch_clause": "catch",
        "synchronized_statement": "synchronized",
        "switch_expression": "switch",
        "switch_statement": "switch",
        "lambda_expression": "lambda",
    }

    def _emit_structural_members(
        self,
        source: SourceFile,
        service: str,
        package: str,
        info: _ClassInfo,
        src: bytes,
        result: ParseResult,
        owner_id: str,
    ) -> None:
        """Emit fields, constructors, methods, parameters, annotations, logic blocks."""
        existing = {n.id for n in result.nodes}
        self._emit_annotation_nodes(
            source, service, info.name, owner_id, info.annotations, "type", src, result,
        )
        if info.body is None:
            return
        for field in self._iter_children(info.body, "field_declaration"):
            self._emit_field(source, service, info, field, owner_id, src, result, existing)
        for ctor in self._iter_children(info.body, "constructor_declaration"):
            self._emit_executable(
                source, service, info, ctor, owner_id, src, result, existing,
                kind="constructor",
            )
        for method in self._iter_children(info.body, "method_declaration"):
            self._emit_executable(
                source, service, info, method, owner_id, src, result, existing,
                kind="method",
            )

    def _emit_field(
        self,
        source: SourceFile,
        service: str,
        info: _ClassInfo,
        field: Node,
        owner_id: str,
        src: bytes,
        result: ParseResult,
        existing: set[str],
    ) -> None:
        ann = self._collect_annotations(field, src)
        type_node = field.child_by_field_name("type")
        type_name = self._text(type_node, src) if type_node else "Object"
        modifiers = self._modifiers_text(field, src)
        for declarator in [c for c in field.children if c.type == "variable_declarator"]:
            fname = self._child_field_text(declarator, "name", src) or "field"
            field_id = make_node_id("field", service, f"{info.name}.{fname}")
            if field_id not in existing:
                result.nodes.append(GraphNode(
                    id=field_id, type=NodeType.FIELD, name=fname, service=service,
                    attributes={
                        "class": info.name, "file": source.rel_path,
                        "java_type": type_name, "modifiers": modifiers,
                        "annotations": list(ann.keys()),
                        "start_line": field.start_point[0] + 1,
                        "end_line": field.end_point[0] + 1,
                    },
                ))
                existing.add(field_id)
            result.edges.append(
                GraphEdge(src=owner_id, dst=field_id, type=EdgeType.HAS_FIELD)
            )
            self._emit_annotation_nodes(
                source, service, f"{info.name}.{fname}", field_id, ann, "field", src, result,
            )

    def _emit_executable(
        self,
        source: SourceFile,
        service: str,
        info: _ClassInfo,
        node: Node,
        owner_id: str,
        src: bytes,
        result: ParseResult,
        existing: set[str],
        *,
        kind: str,
    ) -> None:
        m_ann = self._collect_annotations(node, src)
        m_name = self._child_field_text(node, "name", src) or ("<init>" if kind == "constructor" else "unknown")
        ret_type = "void" if kind == "constructor" else (self._child_field_text(node, "type", src) or "void")
        params = self._extract_method_params(node, src)
        thrown = self._extract_throws(node, src)
        modifiers = self._modifiers_text(node, src)
        body_text = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
        if kind == "constructor":
            sig = f"{info.name}({','.join(p['type'] for p in params)})"
            exec_id = make_node_id("constructor", service, f"{info.name}.{sig}")
            ntype = NodeType.CONSTRUCTOR
        else:
            exec_id = make_node_id("method", service, f"{info.name}.{m_name}")
            ntype = NodeType.METHOD
        if exec_id not in existing:
            result.nodes.append(GraphNode(
                id=exec_id, type=ntype, name=m_name, service=service,
                attributes={
                    "class": info.name, "file": source.rel_path, "kind": kind,
                    "returns": ret_type, "parameters": params,
                    "thrown_exceptions": thrown, "modifiers": modifiers,
                    "annotations": list(m_ann.keys()),
                    "line_count": body_text.count("\n") + 1,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "body": body_text[:4000],
                    "called_methods": self._extract_called_methods(node, src),
                },
            ))
            existing.add(exec_id)
            if ntype == NodeType.METHOD:
                result.edges.append(
                    GraphEdge(src=owner_id, dst=exec_id, type=EdgeType.DECLARED_IN)
                )
        if ntype == NodeType.CONSTRUCTOR:
            result.edges.append(
                GraphEdge(src=owner_id, dst=exec_id, type=EdgeType.DECLARED_IN)
            )
        self._emit_annotation_nodes(
            source, service, f"{info.name}.{m_name}", exec_id, m_ann, kind, src, result,
        )
        for param in params:
            param_id = make_node_id(
                "parameter", service, f"{info.name}.{m_name}.{param['name']}",
            )
            if param_id not in existing:
                result.nodes.append(GraphNode(
                    id=param_id, type=NodeType.PARAMETER, name=param["name"],
                    service=service,
                    attributes={
                        "class": info.name, "method": m_name, "file": source.rel_path,
                        "java_type": param["type"],
                        "annotations": param.get("annotations") or [],
                    },
                ))
                existing.add(param_id)
            result.edges.append(
                GraphEdge(src=exec_id, dst=param_id, type=EdgeType.HAS_PARAMETER)
            )
            param_anns = {name: {} for name in param.get("annotations") or []}
            self._emit_annotation_nodes(
                source, service, f"{info.name}.{m_name}.{param['name']}",
                param_id, param_anns, "parameter", src, result,
            )
        for block in self._extract_logical_blocks(node, src):
            block_id = make_node_id(
                "logical_block", service,
                f"{info.name}.{m_name}.{block['kind']}:{block['start_line']}",
            )
            if block_id not in existing:
                result.nodes.append(GraphNode(
                    id=block_id, type=NodeType.LOGICAL_BLOCK, name=block["kind"],
                    service=service,
                    attributes={
                        "class": info.name, "method": m_name, "file": source.rel_path,
                        **block,
                    },
                ))
                existing.add(block_id)
            result.edges.append(
                GraphEdge(src=exec_id, dst=block_id, type=EdgeType.CONTAINS_BLOCK)
            )

    def _emit_annotation_nodes(
        self,
        source: SourceFile,
        service: str,
        owner_name: str,
        owner_id: str,
        annotations: dict[str, dict[str, Any]],
        site: str,
        src: bytes,
        result: ParseResult,
    ) -> None:
        for name, args in annotations.items():
            ann_id = make_node_id("annotation", service, f"{owner_name}@{name}")
            if not any(n.id == ann_id for n in result.nodes):
                result.nodes.append(GraphNode(
                    id=ann_id, type=NodeType.ANNOTATION_USAGE, name=name, service=service,
                    attributes={
                        "file": source.rel_path, "owner": owner_name, "site": site,
                        "arguments": args,
                    },
                ))
            result.edges.append(
                GraphEdge(src=owner_id, dst=ann_id, type=EdgeType.HAS_ANNOTATION,
                          attributes={"annotation": name})
            )

    def _modifiers_text(self, node: Node, src: bytes) -> str:
        mod_node = (
            node.child_by_field_name("modifiers")
            or next((c for c in node.children if c.type == "modifiers"), None)
        )
        return self._text(mod_node, src) if mod_node else ""

    def _extract_logical_blocks(self, method: Node, src: bytes) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for child in self._iter_descendants(method, set(self._LOGIC_BLOCK_TYPES)):
            kind = self._LOGIC_BLOCK_TYPES.get(child.type)
            if not kind:
                continue
            excerpt = src[child.start_byte:child.end_byte].decode("utf-8", "replace")
            condition = ""
            cond_node = child.child_by_field_name("condition")
            if cond_node is not None:
                condition = self._text(cond_node, src)
            blocks.append({
                "kind": kind,
                "start_line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "condition": condition[:500],
                "excerpt": excerpt[:800],
            })
        return blocks

    # ------------------------------------------------------------------ #
    # Call-graph helpers
    # ------------------------------------------------------------------ #
    def _extract_field_injections(self, info: _ClassInfo, src: bytes) -> dict[str, str]:
        """Return {field_name: TypeName} for @Autowired / constructor-injected fields."""
        result: dict[str, str] = {}
        if info.body is None:
            return result
        for field in self._iter_children(info.body, "field_declaration"):
            ann = self._collect_annotations(field, src)
            # Accept @Autowired fields and also final fields (constructor injection).
            is_injected = "Autowired" in ann or "Inject" in ann
            type_node = field.child_by_field_name("type")
            if type_node is None:
                continue
            type_name = self._text(type_node, src).split("<")[0].strip()
            if not is_injected:
                # Heuristic: field named *Service, *Repository, *Client, *Template
                declarators = [c for c in field.children if c.type == "variable_declarator"]
                for d in declarators:
                    fname = self._child_field_text(d, "name", src) or ""
                    if re.search(r"(Service|Repository|Repo|Client|Template|Mapper)$",
                                 type_name, re.IGNORECASE):
                        result[fname] = type_name
            else:
                for d in [c for c in field.children if c.type == "variable_declarator"]:
                    fname = self._child_field_text(d, "name", src) or ""
                    if fname:
                        result[fname] = type_name
        return result

    def _method_delegations(self, method: Node, src: bytes,
                            injections: dict[str, str]) -> list[str]:
        """Return unique type names of injected fields whose methods are called."""
        if not injections:
            return []
        called: set[str] = set()
        for inv in self._iter_descendants(method, {"method_invocation"}):
            obj = inv.child_by_field_name("object")
            if obj is None:
                continue
            obj_name = self._text(obj, src).split(".")[0]
            if obj_name in injections:
                called.add(injections[obj_name])
        return sorted(called)

    def _emit_delegation_edges(self, service: str, src_id: str,
                               injections: dict[str, str],
                               result: ParseResult) -> None:
        """Emit DELEGATES_TO edges from src_id to each injected type node."""
        seen: set[str] = set()
        for type_name in injections.values():
            if type_name in seen:
                continue
            seen.add(type_name)
            if "Repository" in type_name or "Repo" in type_name:
                dst_id = make_node_id("repository", service, type_name)
            else:
                dst_id = make_node_id("service_layer", service, type_name)
            result.edges.append(
                GraphEdge(src=src_id, dst=dst_id, type=EdgeType.DELEGATES_TO,
                          attributes={"injected_type": type_name})
            )

    def _extract_method_params(self, method: Node, src: bytes) -> list[dict]:
        """Return [{name, type, annotations}] for all formal parameters."""
        params_node = method.child_by_field_name("parameters")
        if params_node is None:
            return []
        params = []
        for param in params_node.children:
            if param.type not in ("formal_parameter", "spread_parameter"):
                continue
            ann = self._collect_annotations(param, src)
            type_node = param.child_by_field_name("type")
            name_node = param.child_by_field_name("name")
            params.append({
                "name": self._text(name_node, src) if name_node else "arg",
                "type": self._text(type_node, src) if type_node else "Object",
                "annotations": list(ann.keys()),
            })
        return params

    def _extract_called_methods(self, method: Node, src: bytes) -> list[str]:
        """Return list of 'object.method' or 'method' call strings in this method body."""
        calls: list[str] = []
        seen: set[str] = set()
        for inv in self._iter_descendants(method, {"method_invocation"}):
            obj_node = inv.child_by_field_name("object")
            name_node = inv.child_by_field_name("name")
            if name_node is None:
                continue
            m_name = self._text(name_node, src)
            if obj_node:
                obj = self._text(obj_node, src).split("\n")[0][:40]
                call_str = f"{obj}.{m_name}"
            else:
                call_str = m_name
            if call_str not in seen:
                seen.add(call_str)
                calls.append(call_str)
        return calls[:30]  # cap to avoid huge payloads

    def _extract_local_variables(self, method: Node, src: bytes) -> list[dict]:
        """Return [{name, type}] for local variable declarations in method body."""
        result = []
        seen: set[str] = set()
        for decl in self._iter_descendants(method, {"local_variable_declaration"}):
            type_node = decl.child_by_field_name("type")
            type_str = self._text(type_node, src) if type_node else "var"
            for d in [c for c in decl.children if c.type == "variable_declarator"]:
                name_node = d.child_by_field_name("name")
                name = self._text(name_node, src) if name_node else ""
                if name and name not in seen:
                    seen.add(name)
                    result.append({"name": name, "type": type_str})
        return result[:20]

    # ------------------------------------------------------------------ #
    # Endpoint metadata extractors
    # ------------------------------------------------------------------ #
    def _extract_request_body(self, method: Node, src: bytes) -> str | None:
        params = method.child_by_field_name("parameters")
        if params is None:
            return None
        for param in params.children:
            if param.type != "formal_parameter":
                continue
            ann = self._collect_annotations(param, src)
            if "RequestBody" in ann:
                t = param.child_by_field_name("type")
                if t:
                    return self._text(t, src).split("<")[0].strip()
        return None

    def _extract_path_variables(self, method: Node, src: bytes) -> list[str]:
        params = method.child_by_field_name("parameters")
        if params is None:
            return []
        out = []
        for param in params.children:
            if param.type != "formal_parameter":
                continue
            ann = self._collect_annotations(param, src)
            if "PathVariable" in ann:
                pv = ann["PathVariable"]
                name = pv.get("value") or pv.get("name")
                if not name:
                    name_node = param.child_by_field_name("name")
                    name = self._text(name_node, src) if name_node else "?"
                out.append(str(name).strip('"'))
        return out

    def _extract_query_params(self, method: Node, src: bytes) -> list[dict]:
        params = method.child_by_field_name("parameters")
        if params is None:
            return []
        out = []
        for param in params.children:
            if param.type != "formal_parameter":
                continue
            ann = self._collect_annotations(param, src)
            if "RequestParam" in ann:
                rp = ann["RequestParam"]
                name = rp.get("value") or rp.get("name")
                if not name:
                    n = param.child_by_field_name("name")
                    name = self._text(n, src) if n else "?"
                required = rp.get("required", True)
                default = rp.get("defaultValue")
                out.append({"name": str(name).strip('"'),
                            "required": required, "default": default})
        return out

    def _extract_throws(self, method: Node, src: bytes) -> list[str]:
        throws: list[str] = []
        for child in method.children:
            if child.type == "throws":
                for t in self._iter_descendants(child, {"type_identifier"}):
                    throws.append(self._text(t, src))
        # Also scan body for `throw new XxxException(`.
        body = method.child_by_field_name("body")
        if body:
            body_text = src[body.start_byte:body.end_byte].decode("utf-8", "replace")
            for m in re.finditer(r"throw\s+new\s+([A-Za-z0-9_]+Exception)", body_text):
                exc = m.group(1)
                if exc not in throws:
                    throws.append(exc)
        return throws

    def _extract_value_properties(self, info: _ClassInfo, src: bytes) -> list[str]:
        """Collect @Value("${...}") property keys used in this class."""
        if info.body is None:
            return []
        props: list[str] = []
        class_text = src[info.body.start_byte:info.body.end_byte].decode("utf-8", "replace")
        for m in re.finditer(r'@Value\(\s*"\$\{([^}]+)\}', class_text):
            props.append(m.group(1))
        return sorted(set(props))

    def _extract_repository_methods(self, info: _ClassInfo, src: bytes) -> list[dict]:
        """Extract public method signatures from Repository interfaces."""
        methods = []
        if info.body is None:
            return methods
        for method in self._iter_children(info.body, "method_declaration"):
            m_name = self._child_field_text(method, "name", src) or "unknown"
            m_ann = self._method_annotations(method, src)
            query = None
            if "Query" in m_ann:
                q = m_ann["Query"]
                query = q.get("value") or q.get("nativeQuery") or str(q)
            ret_type = self._child_field_text(method, "type", src) or "void"
            sig = src[method.start_byte:method.end_byte].decode("utf-8", "replace")
            # Keep only the signature (first line or up to opening brace).
            sig_line = sig.split("{")[0].strip().split("\n")[0].strip()
            methods.append({"name": m_name, "signature": sig_line,
                            "returns": ret_type, "query": query})
        return methods

    def _extract_dto_fields(self, info: _ClassInfo, src: bytes) -> list[str]:
        """Return field names declared in a DTO/record class."""
        if info.body is None:
            return []
        fields = []
        for field in self._iter_children(info.body, "field_declaration"):
            for d in [c for c in field.children if c.type == "variable_declarator"]:
                fname = self._child_field_text(d, "name", src)
                if fname:
                    fields.append(fname)
        if info.kind == "record":
            header_text = src[info.body.start_byte - min(300, info.body.start_byte):
                               info.body.start_byte].decode("utf-8", "replace")
            fields += re.findall(r"\b(\w+)\s*[,)]", header_text)
        return list(dict.fromkeys(fields))

    def _extract_dto_fields_full(self, info: _ClassInfo, src: bytes) -> list[dict]:
        """Return rich field metadata: name, type, validations, nullable."""
        if info.body is None:
            return []
        result = []
        for field_node in self._iter_children(info.body, "field_declaration"):
            ann = self._collect_annotations(field_node, src)
            validations = {a: v for a, v in ann.items() if a in _VALIDATION_ANN}
            nullable = "NotNull" not in ann and "NotBlank" not in ann
            type_node = field_node.child_by_field_name("type")
            type_str = self._text(type_node, src) if type_node else "Object"
            for d in [c for c in field_node.children if c.type == "variable_declarator"]:
                fname = self._child_field_text(d, "name", src)
                if fname:
                    result.append({"name": fname, "type": type_str,
                                   "nullable": nullable, "validations": validations})
        # Record components carry their own formal parameters.
        if info.kind == "record" and info.body:
            header = src[max(0, info.body.start_byte - 400):info.body.start_byte]\
                         .decode("utf-8", "replace")
            for m in re.finditer(r"(\w[\w<>, ]*?)\s+(\w+)\s*[,)]", header):
                type_str, fname = m.group(1).strip(), m.group(2).strip()
                if fname and not any(f["name"] == fname for f in result):
                    result.append({"name": fname, "type": type_str,
                                   "nullable": False, "validations": {}})
        return result

    def _generate_json_example(self, fields: list[dict]) -> dict:
        """Generate a plausible JSON example from DTO field metadata."""
        example: dict[str, Any] = {}
        for f in fields:
            name = f.get("name", "field")
            raw_type = f.get("type", "String")
            base_type = re.sub(r"<.*>", "", raw_type).strip().split(".")[-1]
            if base_type in ("List", "Set", "Collection"):
                example[name] = []
            elif base_type in ("Map",):
                example[name] = {}
            elif f.get("nullable"):
                example[name] = _JAVA_TYPE_EXAMPLES.get(base_type, None)
            else:
                example[name] = _JAVA_TYPE_EXAMPLES.get(base_type, f"<{base_type}>")
        return example

    def _detect_design_patterns(self, info: _ClassInfo) -> list[str]:
        """Return a list of design pattern names detected in this class."""
        detected: list[str] = []
        ann_keys = set(info.annotations.keys())
        name = info.name
        kind = info.kind

        for pattern, det_type, signal in _PATTERN_SIGNALS:
            if pattern in detected:
                continue
            if det_type == "annotation" and signal in ann_keys or det_type == "name_suffix" and name.endswith(signal) or det_type == "kind" and signal in kind:
                detected.append(pattern)

        if "Observer / Event" not in detected and any(
            a in ann_keys for a in ("KafkaListener", "RabbitListener", "JmsListener")
        ):
            detected.append("Observer / Event")
        return detected

    # ------------------------------------------------------------------ #
    # Entity schema extraction
    # ------------------------------------------------------------------ #
    def _extract_entity_schema(
        self, info: _ClassInfo, src: bytes
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Return (columns, relationships, antipatterns) for a JPA entity."""
        columns: list[dict] = []
        relationships: list[dict] = []
        antipatterns: list[str] = []

        if info.body is None:
            return columns, relationships, antipatterns

        _REL_ANN = {"OneToOne", "OneToMany", "ManyToOne", "ManyToMany"}

        for field_node in self._iter_children(info.body, "field_declaration"):
            ann = self._collect_annotations(field_node, src)
            type_node = field_node.child_by_field_name("type")
            type_str = self._text(type_node, src) if type_node else "Object"
            base_type = re.sub(r"<.*>", "", type_str).strip()

            for d in [c for c in field_node.children if c.type == "variable_declarator"]:
                fname = self._child_field_text(d, "name", src)
                if not fname:
                    continue

                rel_types = _REL_ANN & ann.keys()
                if rel_types:
                    rel_type = next(iter(rel_types))
                    target = re.sub(r"List|Set|Collection|Optional", "", base_type).strip("<>") or base_type
                    fetch = ann.get(rel_type, {}).get("fetch", "EAGER") if isinstance(ann.get(rel_type), dict) else "EAGER"
                    cascade = ann.get(rel_type, {}).get("cascade", []) if isinstance(ann.get(rel_type), dict) else []

                    # N+1 risk: EAGER on @OneToMany or @ManyToMany
                    if rel_type in ("OneToMany", "ManyToMany") and fetch == "EAGER":
                        antipatterns.append(
                            f"N+1 risk: {fname} is {rel_type} EAGER — use LAZY + JOIN FETCH"
                        )
                    relationships.append({
                        "field": fname, "type": rel_type, "target_entity": target,
                        "fetch": fetch, "cascade": cascade,
                    })
                else:
                    col_info: dict = {"field": fname, "java_type": type_str}
                    if "Column" in ann:
                        col_info.update({
                            "nullable": True,  # default
                            "unique": False,
                        })
                    if "Id" in ann:
                        col_info["primary_key"] = True
                    if "GeneratedValue" in ann:
                        col_info["generated"] = True
                    if "NotNull" in ann or "Column" in ann:
                        col_info["nullable"] = False
                    if "Lob" in ann:
                        col_info["lob"] = True
                    columns.append(col_info)

        return columns, relationships, antipatterns

    # ------------------------------------------------------------------ #
    # Antipattern detection
    # ------------------------------------------------------------------ #
    def _detect_antipatterns(
        self,
        info: _ClassInfo,
        src: bytes,
        is_controller: bool,
        is_service_layer: bool,
        is_entity: bool,
        injections: dict[str, str],
        config_props: list[str],
    ) -> list[str]:
        smells: list[str] = []
        ann = info.annotations
        body_text = src[info.body.start_byte:info.body.end_byte].decode("utf-8", "replace") \
            if info.body else ""
        method_count = body_text.count("public ") + body_text.count("private ")

        # God class.
        if method_count > 20:
            smells.append(f"God class: {method_count} methods — consider splitting")

        # @Transactional on @RestController.
        if is_controller and "Transactional" in ann:
            smells.append("@Transactional on @RestController — move to service layer")

        # Missing @Valid on controller request bodies.
        if is_controller and "@Valid" not in body_text and "RequestBody" in body_text:
            smells.append("Missing @Valid on @RequestBody — input not validated")

        # Field injection (@Autowired on fields instead of constructor).
        if "@Autowired" in body_text and "public " + info.name + "(" not in body_text:
            smells.append("Field injection via @Autowired — prefer constructor injection")

        # Catching generic Exception/Throwable.
        if re.search(r"catch\s*\(\s*(Exception|Throwable)\s+\w+\)", body_text):
            smells.append("Catching generic Exception/Throwable — use specific exception types")

        # System.out / e.printStackTrace.
        if "System.out.print" in body_text or "e.printStackTrace" in body_text:
            smells.append("Direct System.out or printStackTrace — use SLF4J logger")

        # Missing @Transactional on write service methods.
        if is_service_layer and "Transactional" not in ann:
            write_ops = re.findall(r"(save|delete|update|create|persist|merge|remove)\w*\s*\(", body_text, re.IGNORECASE)
            if write_ops:
                smells.append(
                    f"Service has write operations ({', '.join(set(write_ops[:3]))}) "
                    "but no class-level @Transactional"
                )

        # Hardcoded strings that look like URLs or credentials.
        if re.search(r'"https?://[^"]{10,}"', body_text):
            smells.append("Hardcoded URL in class body — externalise to config")
        if re.search(r'"[A-Za-z0-9]{16,}"', body_text) and is_service_layer:
            smells.append("Possible hardcoded secret/token — externalise to Vault/env")

        # N+1 in service: calling repo inside a loop.
        if is_service_layer and re.search(r"for\s*\(.*\)\s*\{[^}]*Repository\.", body_text, re.DOTALL):
            smells.append("Possible N+1: repository call inside loop — use batch fetch")

        return smells

    # ------------------------------------------------------------------ #
    # Exception / test helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _exception_http_status(name: str) -> str:
        mapping = {
            "notfound": "404", "notexist": "404",
            "badrequest": "400", "invalid": "400", "validation": "400",
            "unauthorized": "401", "unauthenticated": "401",
            "forbidden": "403", "access": "403",
            "conflict": "409", "duplicate": "409",
            "internal": "500",
        }
        lower = name.lower()
        for k, v in mapping.items():
            if k in lower:
                return v
        return "500"

    @staticmethod
    def _infer_tested_class(info: _ClassInfo) -> str | None:
        name = info.name
        for suffix in ("Test", "Tests", "IT", "Spec", "IntegrationTest"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return None

    @staticmethod
    def _test_framework(ann: dict) -> str:
        if "SpringBootTest" in ann or "WebMvcTest" in ann or "DataJpaTest" in ann:
            return "spring-boot-test"
        if "ExtendWith" in ann or "MockitoExtension" in ann:
            return "mockito-junit5"
        if "RunWith" in ann:
            return "junit4"
        return "unknown"

    @staticmethod
    def _test_type(ann: dict) -> str:
        if "WebMvcTest" in ann:
            return "controller-slice"
        if "DataJpaTest" in ann:
            return "repository-slice"
        if "SpringBootTest" in ann:
            return "integration"
        return "unit"

    @staticmethod
    def _build_chain_header(
        ep_name: str, controller: str, handler: str,
        request_body: str | None, path_vars: list[str],
        query_params: list[dict], ret_type: str,
        thrown_exceptions: list[str], transactional: bool,
        security: str | None, delegates_to: list[str],
    ) -> str:
        lines = [
            f"// ═══ ENDPOINT: {ep_name} ═══",
            f"// Controller : {controller}.{handler}()",
            f"// Request    : body={request_body or 'none'}"
            f"  pathVars={path_vars or []}  queryParams={[p['name'] for p in query_params]}",
            f"// Response   : {ret_type}  status={None}",
            f"// Delegates  : {delegates_to or ['(direct)']}",
            f"// Throws     : {thrown_exceptions or ['none']}",
            f"// Transact.  : {transactional}  security={security or 'none'}",
            "// ─────────────────────────────────────────────────────────",
        ]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #
    # Kafka
    # ------------------------------------------------------------------ #
    def _emit_kafka(
        self, source: SourceFile, service: str, info: _ClassInfo, src: bytes,
        result: ParseResult,
    ) -> None:
        if info.body is None:
            return
        for method in self._iter_children(info.body, "method_declaration"):
            m_annotations = self._method_annotations(method, src)
            if "KafkaListener" in m_annotations:
                topics = self._listener_topics(m_annotations["KafkaListener"])
                for topic in topics:
                    self._add_topic_edge(
                        service, info.name, topic, EdgeType.CONSUMES_FROM,
                        NodeType.KAFKA_CONSUMER, result,
                    )
        # Producer heuristic: kafkaTemplate.send("topic", ...) call literals.
        file_text = src.decode("utf-8", "replace")
        for topic in set(re.findall(r"\.send\(\s*\"([A-Za-z0-9._\-]+)\"", file_text)):
            self._add_topic_edge(
                service, info.name, topic, EdgeType.PRODUCES_TO,
                NodeType.KAFKA_PRODUCER, result,
            )

    def _add_topic_edge(
        self, service: str, cls: str, topic: str, edge: EdgeType,
        node_type: NodeType, result: ParseResult,
    ) -> None:
        actor_id = make_node_id(node_type.value, service, cls)
        topic_id = make_node_id("topic", "_shared", topic)
        result.nodes.append(
            GraphNode(id=actor_id, type=node_type, name=cls, service=service,
                      attributes={"topic": topic})
        )
        result.nodes.append(
            GraphNode(id=topic_id, type=NodeType.TOPIC, name=topic, service=None,
                      attributes={})
        )
        result.edges.append(GraphEdge(src=actor_id, dst=topic_id, type=edge))

    # ------------------------------------------------------------------ #
    # Tree-sitter helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _text(node: Node | None, src: bytes) -> str:
        if node is None:
            return ""
        return src[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def _package_name(self, root: Node, src: bytes) -> str:
        for child in root.children:
            if child.type == "package_declaration":
                for sub in child.children:
                    if sub.type in ("scoped_identifier", "identifier"):
                        return self._text(sub, src)
        return ""

    def _collect_classes(self, root: Node, src: bytes) -> list[_ClassInfo]:
        out: list[_ClassInfo] = []
        stack = [root]
        wanted = {"class_declaration", "interface_declaration",
                  "enum_declaration", "record_declaration"}
        while stack:
            node = stack.pop()
            for child in node.children:
                if child.type in wanted:
                    out.append(self._class_info(child, src))
                stack.append(child)
        return out

    def _class_info(self, node: Node, src: bytes) -> _ClassInfo:
        kind = node.type.replace("_declaration", "")
        name = self._child_field_text(node, "name", src) or "Anonymous"
        annotations = self._collect_annotations(node, src)
        supertypes = self._supertypes(node, src)
        body = node.child_by_field_name("body")
        header_end = body.start_byte if body is not None else node.end_byte
        header = src[node.start_byte:header_end].decode("utf-8", "replace")
        return _ClassInfo(
            name=name, kind=kind, annotations=annotations, supertypes=supertypes,
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            body=body, header=header,
        )

    def _collect_annotations(self, node: Node, src: bytes) -> dict[str, dict[str, Any]]:
        """Type-level annotations from the ``modifiers`` child."""
        result: dict[str, dict[str, Any]] = {}
        modifiers = None
        for child in node.children:
            if child.type == "modifiers":
                modifiers = child
                break
        if modifiers is None:
            return result
        for child in modifiers.children:
            if child.type in ("annotation", "marker_annotation"):
                name, args = self._parse_annotation(child, src)
                result[name] = args
        return result

    def _parse_annotation(self, node: Node, src: bytes) -> tuple[str, dict[str, Any]]:
        name_node = node.child_by_field_name("name")
        name = self._text(name_node, src).lstrip("@").split(".")[-1]
        args: dict[str, Any] = {}
        arg_list = node.child_by_field_name("arguments")
        if arg_list is not None:
            for child in arg_list.children:
                if child.type == "element_value_pair":
                    key = self._child_field_text(child, "key", src)
                    val = self._annotation_value(child.child_by_field_name("value"), src)
                    if key:
                        args[key] = val
                elif child.type in ("string_literal", "array_initializer",
                                    "field_access", "identifier"):
                    args.setdefault("value", self._annotation_value(child, src))
        return name, args

    def _annotation_value(self, node: Node | None, src: bytes) -> Any:
        if node is None:
            return None
        if node.type == "string_literal":
            return self._text(node, src).strip('"')
        if node.type == "array_initializer":
            vals = []
            for c in node.children:
                if c.type == "string_literal":
                    vals.append(self._text(c, src).strip('"'))
            return vals
        return self._text(node, src)

    def _supertypes(self, node: Node, src: bytes) -> list[str]:
        """Ordered type names from ``extends``/``implements`` clauses.

        Collected from every header child that is not the class body, so it works
        for both ``class ... extends/implements`` and ``interface ... extends``
        regardless of the exact grammar field name.
        """
        body = node.child_by_field_name("body")
        body_start = body.start_byte if body is not None else node.end_byte
        name_node = node.child_by_field_name("name")
        own_name = self._text(name_node, src) if name_node is not None else ""
        out: list[str] = []
        for t in self._iter_descendants(node, {"type_identifier"}):
            if t.start_byte >= body_start:
                continue
            text = self._text(t, src)
            if text and text != own_name:
                out.append(text)
        return out

    def _method_annotations(self, method: Node, src: bytes) -> dict[str, dict[str, Any]]:
        return self._collect_annotations(method, src)

    def _param_types(self, method: Node, src: bytes) -> list[str]:
        params = method.child_by_field_name("parameters")
        if params is None:
            return []
        out: list[str] = []
        for child in params.children:
            if child.type in ("formal_parameter", "spread_parameter"):
                t = child.child_by_field_name("type")
                if t is not None:
                    out.append(self._text(t, src))
        return out

    def _child_field_text(self, node: Node, field_name: str, src: bytes) -> str | None:
        child = node.child_by_field_name(field_name)
        return self._text(child, src) if child is not None else None

    @staticmethod
    def _iter_children(node: Node, type_name: str):
        for child in node.children:
            if child.type == type_name:
                yield child

    def _iter_descendants(self, node: Node, types: set[str]):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in types:
                yield n
            stack.extend(n.children)

    # ------------------------------------------------------------------ #
    # Semantic helpers
    # ------------------------------------------------------------------ #
    def _request_mapping_path(self, ann: dict[str, dict[str, Any]]) -> str:
        rm = ann.get("RequestMapping")
        if not rm:
            return ""
        val = rm.get("value") or rm.get("path")
        if isinstance(val, list):
            val = val[0] if val else ""
        return val or ""

    def _method_http(self, ann: dict[str, dict[str, Any]]) -> tuple[str | None, str]:
        for name, http in _HTTP_MAPPING.items():
            if name in ann:
                return http, self._first_path(ann[name])
        if "RequestMapping" in ann:
            rm = ann["RequestMapping"]
            method = rm.get("method", "GET")
            http = re.sub(r".*\.", "", str(method)).upper() or "GET"
            if http not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                http = "GET"
            return http, self._first_path(rm)
        return None, ""

    @staticmethod
    def _first_path(args: dict[str, Any]) -> str:
        val = args.get("value") or args.get("path") or ""
        if isinstance(val, list):
            return val[0] if val else ""
        return val

    @staticmethod
    def _join_path(base: str, sub: str) -> str:
        base = "/" + base.strip("/") if base else ""
        sub = "/" + sub.strip("/") if sub else ""
        path = (base + sub) or "/"
        return re.sub(r"//+", "/", path) or "/"

    def _table_name(self, ann: dict[str, dict[str, Any]], class_name: str) -> str:
        table = ann.get("Table")
        if table and table.get("name"):
            return str(table["name"]).strip('"')
        # Fallback: Spring's default snake_case naming strategy.
        return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()

    def _looks_like_dto(self, info: _ClassInfo) -> bool:
        if info.kind == "record":
            return True
        return bool(re.search(r"(Dto|DTO|Request|Response|Payload|Vo|VO)$", info.name))

    def _repository_entity(self, info: _ClassInfo) -> str | None:
        # Extract the first type argument of the repository supertype, e.g.
        # JpaRepository<Order, Long> -> "Order". Regex on the (order-preserving)
        # header text is more reliable than the DFS-ordered supertypes list.
        m = re.search(
            r"(?:JpaRepository|CrudRepository|PagingAndSortingRepository|"
            r"MongoRepository|ReactiveCrudRepository)\s*<\s*([A-Za-z0-9_.]+)",
            info.header,
        )
        return m.group(1).split(".")[-1] if m else None

    def _feign_target(self, ann: dict[str, dict[str, Any]]) -> str | None:
        fc = ann.get("FeignClient", {})
        target = fc.get("name") or fc.get("value")
        if isinstance(target, str):
            return target.strip('"')
        return None

    def _listener_topics(self, args: dict[str, Any]) -> list[str]:
        topics = args.get("topics") or args.get("value") or []
        if isinstance(topics, str):
            return [topics.strip('"')]
        if isinstance(topics, list):
            return [str(t).strip('"') for t in topics]
        return []

    @staticmethod
    def _dto_type_names(type_expr: str) -> list[str]:
        # Pull DTO-looking identifiers out of e.g. ResponseEntity<List<OrderDto>>.
        names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", type_expr or "")
        return [n for n in names if re.search(r"(Dto|DTO|Request|Response|Payload)$", n)]

    def _class_start_byte(self, info: _ClassInfo) -> int:
        return max(0, info.body.start_byte - 200) if info.body else 0

    def _class_end_byte(self, info: _ClassInfo) -> int:
        return info.body.end_byte if info.body else 0
