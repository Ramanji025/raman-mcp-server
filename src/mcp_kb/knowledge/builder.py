"""Build semantic knowledge objects from parser outputs."""
from __future__ import annotations

import re
from collections import defaultdict

from ..models import EdgeType, NodeType, ParseResult
from .models import (
    KnowledgeKind,
    KnowledgeMethodContract,
    KnowledgeObject,
    MethodInput,
    MethodOutput,
)

_SQL_PATTERN = re.compile(
    r"\b(select|insert|update|delete|merge|upsert|create|alter|drop)\b[\s\S]{0,300}",
    re.IGNORECASE,
)
_API_CALL_PATTERN = re.compile(
    r"(restTemplate\.[A-Za-z0-9_]+|webClient\.[A-Za-z0-9_]+|feign[A-Za-z0-9_]*\.[A-Za-z0-9_]+|http[A-Za-z0-9_]*\.[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_CATCH_PATTERN = re.compile(r"catch\s*\(\s*([A-Za-z0-9_$.]+)")


class KnowledgeBuilder:
    """Transforms ParseResult into rich semantic knowledge objects."""

    def build(
        self,
        repository: str,
        parsed: ParseResult,
        source_paths: list[str],
    ) -> list[KnowledgeObject]:
        """Build KnowledgeObject records (repo/microservice/classes/methods) from one parse result."""
        microservice = repository
        objects: list[KnowledgeObject] = []

        objects.append(
            KnowledgeObject(
                id=f"ko:repo:{repository}",
                kind=KnowledgeKind.REPOSITORY,
                repository=repository,
                microservice=microservice,
                name=repository,
                qualified_name=repository,
                attributes={"source_files": sorted(set(source_paths))},
            )
        )
        objects.append(
            KnowledgeObject(
                id=f"ko:microservice:{microservice}",
                kind=KnowledgeKind.MICROSERVICE,
                repository=repository,
                microservice=microservice,
                name=microservice,
                qualified_name=microservice,
            )
        )

        module_paths = sorted({self._module_from_path(p) for p in source_paths if self._module_from_path(p)})
        for module in module_paths:
            objects.append(
                KnowledgeObject(
                    id=f"ko:module:{repository}:{module}",
                    kind=KnowledgeKind.MODULE,
                    repository=repository,
                    microservice=microservice,
                    module=module,
                    name=module,
                    qualified_name=f"{repository}:{module}",
                )
            )

        package_names: set[str] = set()
        method_nodes: dict[str, dict] = {}
        repository_nodes: dict[str, dict] = {}
        entity_to_tables: dict[str, set[str]] = defaultdict(set)

        for node in parsed.nodes:
            attrs = node.attributes or {}
            package = attrs.get("package")
            if isinstance(package, str) and package:
                package_names.add(package)

            module = self._module_from_path(attrs.get("file", ""))
            ko = self._node_to_knowledge(repository, microservice, node, module)
            if ko:
                objects.append(ko)

            if node.type == NodeType.METHOD:
                method_nodes[node.id] = {
                    "name": node.name,
                    "attrs": attrs,
                    "module": module,
                    "package": package,
                }
            if node.type == NodeType.REPOSITORY:
                repository_nodes[node.name] = {"id": node.id, "attrs": attrs}

        for package in sorted(package_names):
            objects.append(
                KnowledgeObject(
                    id=f"ko:package:{repository}:{package}",
                    kind=KnowledgeKind.PACKAGE,
                    repository=repository,
                    microservice=microservice,
                    package=package,
                    name=package,
                    qualified_name=package,
                )
            )

        for edge in parsed.edges:
            if edge.type == EdgeType.MAPS_TO and edge.src.startswith("entity:") and edge.dst.startswith("table:"):
                entity_name = edge.src.split(":")[-1]
                table_name = edge.dst.split(":")[-1]
                entity_to_tables[entity_name].add(table_name)

        callers = self._build_callers(method_nodes)
        method_objects = self._method_knowledge(
            repository,
            microservice,
            method_nodes,
            callers,
            repository_nodes,
            entity_to_tables,
        )
        objects.extend(method_objects)

        objects.extend(self._sql_objects(repository, microservice, parsed))
        return objects

    def _node_to_knowledge(
        self,
        repository: str,
        microservice: str,
        node,
        module: str | None,
    ) -> KnowledgeObject | None:
        attrs = node.attributes or {}
        file_path = attrs.get("file")
        kind_map = {
            NodeType.BUSINESS_CAPABILITY: KnowledgeKind.BUSINESS_CAPABILITY,
            NodeType.EXECUTION_FLOW: KnowledgeKind.EXECUTION_FLOW,
            NodeType.BUSINESS_FLOW: KnowledgeKind.BUSINESS_FLOW,
            NodeType.TECHNICAL_FLOW: KnowledgeKind.TECHNICAL_FLOW,
            NodeType.ENDPOINT: KnowledgeKind.API_ENDPOINT,
            NodeType.DTO: KnowledgeKind.DTO,
            NodeType.ENTITY: KnowledgeKind.ENTITY,
            NodeType.CONFIG: KnowledgeKind.CONFIGURATION,
            NodeType.CONFIG_PROPERTY: KnowledgeKind.CONFIGURATION,
            NodeType.EXCEPTION_TYPE: KnowledgeKind.EXCEPTION,
            NodeType.CLASS: KnowledgeKind.CLASS,
            NodeType.INTERFACE: KnowledgeKind.INTERFACE,
            NodeType.CONTROLLER: KnowledgeKind.CLASS,
            NodeType.SERVICE_LAYER: KnowledgeKind.CLASS,
            NodeType.REPOSITORY: KnowledgeKind.INTERFACE,
        }
        kind = kind_map.get(node.type)
        if kind is None:
            return None

        class_kind = attrs.get("kind")
        if class_kind == "interface":
            kind = KnowledgeKind.INTERFACE
        elif class_kind in {"class", "record", "enum"} and kind in {KnowledgeKind.INTERFACE, KnowledgeKind.CLASS}:
            kind = KnowledgeKind.CLASS

        qualified_name = attrs.get("fqcn") or node.name
        return KnowledgeObject(
            id=f"ko:{kind.value}:{repository}:{qualified_name}",
            kind=kind,
            repository=repository,
            microservice=microservice,
            module=module,
            package=attrs.get("package"),
            name=node.name,
            qualified_name=qualified_name,
            file_path=file_path,
            start_line=attrs.get("start_line"),
            end_line=attrs.get("end_line"),
            attributes=attrs,
        )

    def _build_callers(self, method_nodes: dict[str, dict]) -> dict[str, list[str]]:
        by_short: dict[str, list[str]] = defaultdict(list)
        for node in method_nodes.values():
            fq = f"{node['attrs'].get('class', '')}.{node['name']}"
            by_short[node["name"]].append(fq)

        callers: dict[str, list[str]] = defaultdict(list)
        for node in method_nodes.values():
            caller_name = f"{node['attrs'].get('class', '')}.{node['name']}"
            for callee in node["attrs"].get("called_methods", []):
                short = callee.split(".")[-1]
                for target in by_short.get(short, []):
                    callers[target].append(caller_name)
        return {k: sorted(set(v)) for k, v in callers.items()}

    def _method_knowledge(
        self,
        repository: str,
        microservice: str,
        method_nodes: dict[str, dict],
        callers: dict[str, list[str]],
        repository_nodes: dict[str, dict],
        entity_to_tables: dict[str, set[str]],
    ) -> list[KnowledgeObject]:
        out: list[KnowledgeObject] = []

        for node in method_nodes.values():
            attrs = node["attrs"]
            method_name = node["name"]
            class_name = attrs.get("class", "")
            fq = f"{class_name}.{method_name}"
            parameters = attrs.get("parameters", [])
            inputs = [
                MethodInput(
                    name=str(p.get("name", "arg")),
                    type=str(p.get("type", "Object")),
                    annotations=[str(a) for a in p.get("annotations", [])],
                )
                for p in parameters
            ]

            returns = MethodOutput(type=str(attrs.get("returns", "void")))
            called = [str(c) for c in attrs.get("called_methods", [])]
            thrown = [str(e) for e in attrs.get("thrown_exceptions", [])]
            body = str(attrs.get("body", ""))
            caught = sorted({e.split(".")[-1] for e in _CATCH_PATTERN.findall(body)})
            delegated_repos = [str(r) for r in attrs.get("delegates_to_repos", [])]
            tables = self._tables_for_method(delegated_repos, repository_nodes, entity_to_tables)
            apis_called = sorted({m.group(1) for m in _API_CALL_PATTERN.finditer(" ".join(called) + "\n" + body)})

            method_contract = KnowledgeMethodContract(
                purpose=self._purpose(method_name, called, thrown),
                inputs=inputs,
                outputs=returns,
                callers=callers.get(fq, []),
                callees=called,
                exceptions_thrown=thrown,
                exceptions_caught=caught,
                database_tables_used=tables,
                apis_called=apis_called,
                business_purpose=self._business_purpose(method_name, called, tables, apis_called),
            )

            out.append(
                KnowledgeObject(
                    id=f"ko:method:{repository}:{fq}",
                    kind=KnowledgeKind.METHOD,
                    repository=repository,
                    microservice=microservice,
                    module=node.get("module"),
                    package=node.get("package"),
                    name=method_name,
                    qualified_name=fq,
                    file_path=attrs.get("file"),
                    start_line=attrs.get("start_line"),
                    end_line=attrs.get("end_line"),
                    attributes={"class": class_name, "transactional": attrs.get("transactional", False)},
                    method=method_contract,
                )
            )
        return out

    def _tables_for_method(
        self,
        delegated_repos: list[str],
        repository_nodes: dict[str, dict],
        entity_to_tables: dict[str, set[str]],
    ) -> list[str]:
        tables: set[str] = set()
        for repo_name in delegated_repos:
            repo = repository_nodes.get(repo_name)
            if not repo:
                continue
            for query_method in repo["attrs"].get("query_methods", []):
                query = query_method.get("query")
                if isinstance(query, str):
                    for table in self._sql_tables(query):
                        tables.add(table)
        for entity, mapped_tables in entity_to_tables.items():
            for repo_name in delegated_repos:
                if entity.lower() in repo_name.lower():
                    tables.update(mapped_tables)
        return sorted(tables)

    def _sql_objects(self, repository: str, microservice: str, parsed: ParseResult) -> list[KnowledgeObject]:
        out: list[KnowledgeObject] = []
        seen: set[str] = set()

        for node in parsed.nodes:
            if node.type != NodeType.REPOSITORY:
                continue
            attrs = node.attributes or {}
            file_path = attrs.get("file")
            for method in attrs.get("query_methods", []):
                query = method.get("query")
                if not isinstance(query, str) or not query.strip():
                    continue
                qid = f"ko:sql:{repository}:{node.name}:{method.get('name')}"
                if qid in seen:
                    continue
                seen.add(qid)
                out.append(
                    KnowledgeObject(
                        id=qid,
                        kind=KnowledgeKind.SQL_QUERY,
                        repository=repository,
                        microservice=microservice,
                        name=str(method.get("name", "query")),
                        qualified_name=f"{node.name}.{method.get('name', 'query')}",
                        file_path=file_path,
                        attributes={
                            "query": query,
                            "tables": self._sql_tables(query),
                            "repository": node.name,
                        },
                    )
                )

        for chunk in parsed.chunks:
            if chunk.content_type.value != "flyway":
                continue
            for idx, match in enumerate(_SQL_PATTERN.finditer(chunk.text)):
                query = match.group(0).strip()
                qid = f"ko:sql:{repository}:{chunk.rel_path}:{idx}"
                if qid in seen:
                    continue
                seen.add(qid)
                out.append(
                    KnowledgeObject(
                        id=qid,
                        kind=KnowledgeKind.SQL_QUERY,
                        repository=repository,
                        microservice=microservice,
                        name=f"sql_{idx}",
                        file_path=chunk.rel_path,
                        attributes={"query": query, "tables": self._sql_tables(query)},
                    )
                )
        return out

    @staticmethod
    def _module_from_path(path: str) -> str | None:
        if not path:
            return None
        top = path.replace("\\", "/").split("/", 1)[0].strip()
        return top or None

    @staticmethod
    def _sql_tables(query: str) -> list[str]:
        names = set(re.findall(r"\b(from|join|into|update|table)\s+([A-Za-z0-9_\.]+)", query, re.IGNORECASE))
        return sorted({item[1].split(".")[-1] for item in names})

    @staticmethod
    def _purpose(method_name: str, callees: list[str], thrown: list[str]) -> str:
        lower = method_name.lower()
        if lower.startswith(("get", "find", "fetch", "list", "load", "read")):
            return "Read data for downstream use"
        if lower.startswith(("create", "save", "insert", "register", "submit")):
            return "Create or persist domain data"
        if lower.startswith(("update", "patch", "modify", "change", "set")):
            return "Update existing domain state"
        if lower.startswith(("delete", "remove", "deactivate")):
            return "Delete or deactivate domain state"
        if thrown:
            return f"Business validation with exception path ({thrown[0]})"
        if any("kafka" in c.lower() for c in callees):
            return "Publish or route an integration event"
        return "Coordinate business logic across collaborators"

    @staticmethod
    def _business_purpose(
        method_name: str,
        callees: list[str],
        tables: list[str],
        apis_called: list[str],
    ) -> str:
        pieces = [f"Method {method_name} orchestrates service behavior"]
        if tables:
            pieces.append(f"touches tables: {', '.join(tables[:4])}")
        if apis_called:
            pieces.append(f"calls external APIs: {', '.join(apis_called[:3])}")
        if not tables and not apis_called and callees:
            pieces.append(f"delegates to internal methods: {', '.join(callees[:3])}")
        return "; ".join(pieces)
