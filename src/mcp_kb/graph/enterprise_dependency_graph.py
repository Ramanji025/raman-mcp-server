"""Enterprise dependency graph augmentation.

Builds cross-cutting microservice dependency nodes/edges from parser output:
- Gateway, Service, Endpoint, Kafka Topic, Queue, Database
- CALLS, PUBLISHES, CONSUMES, USES, READS, WRITES, DEPENDS_ON
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import Settings
from ..ingestion.parsers.base import make_node_id
from ..models import EdgeType, GraphEdge, GraphNode, NodeType, ParseResult

_HOST_RE = re.compile(r"https?://([A-Za-z0-9_.-]+)", re.IGNORECASE)
_DB_URL_RE = re.compile(r"^[a-z]+:.*//([^/:?#]+)(?::\d+)?(?:/([^?]+))?", re.IGNORECASE)
_SQL_READ = re.compile(r"\b(select|find|get|read|count|exists)\b", re.IGNORECASE)

_CAPABILITY_RULES: dict[str, tuple[str, ...]] = {
    "Checkout": (
        "checkout", "cart", "place order", "placeorder", "submit order", "buy now",
    ),
    "Customer Onboarding": (
        "onboard", "onboarding", "register", "signup", "sign up", "create customer", "activate account",
    ),
    "Order Processing": (
        "order", "fulfill", "fulfillment", "shipment", "dispatch", "order status",
    ),
    "Inventory Management": (
        "inventory", "stock", "reserve", "reservation", "warehouse", "availability",
    ),
    "Payment Processing": (
        "payment", "pay", "charge", "billing", "invoice", "transaction", "authorize", "validate payment",
    ),
    "Notification Delivery": (
        "notify", "notification", "email", "sms", "webhook", "alert", "message", "publish event",
    ),
}
_SQL_WRITE = re.compile(r"\b(insert|update|delete|merge|upsert|save|remove|create)\b", re.IGNORECASE)


def _service_node_id(name: str) -> str:
    return f"service:{name}:{name}"


def _gateway_node_id(name: str) -> str:
    return f"gateway:{name}:{name}"


def _db_node_id(name: str) -> str:
    return f"database:_shared:{name}"


def _queue_node_id(name: str) -> str:
    return f"queue:_shared:{name}"


class EnterpriseDependencyGraphAugmenter:
    """Adds cross-service edges (HTTP/Feign calls, Kafka aliases, gateways) a single-repo parse can't see."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def augment(self, repo: str, parsed: ParseResult) -> ParseResult:
        """Return a copy of `parsed` augmented with cross-service gateway/HTTP/Feign/Kafka edges."""
        out = ParseResult(
            nodes=list(parsed.nodes),
            edges=list(parsed.edges),
            chunks=list(parsed.chunks),
        )
        service_id = _service_node_id(repo)
        repo_names = self._local_repo_names()

        self._ensure_gateway_node(repo, service_id, out)
        self._add_http_and_feign_calls(repo, service_id, out, repo_names)
        self._add_kafka_alias_edges(out)
        self._add_shared_lib_dependencies(repo, service_id, out, repo_names)
        self._add_database_dependencies(repo, service_id, out)
        self._add_queue_dependencies(repo, service_id, out)
        self._add_repository_table_read_write_edges(out)
        self._add_method_level_call_graph(repo, out)
        self._add_execution_flows(repo, service_id, out)
        self._add_business_capabilities(repo, service_id, out)
        return out

    def _add_method_level_call_graph(self, repo: str, out: ParseResult) -> None:
        """Resolve parser call expressions into ``Method -[:CALLS]-> Method`` edges.

        Calls on injected fields resolve through the retained field-to-type map;
        direct calls resolve within the declaring class. Repository calls without
        explicit source declarations receive an inferred method node, preserving
        common Spring Data operations such as ``repository.save()``.
        """
        methods = [node for node in out.nodes if node.type == NodeType.METHOD]
        methods_by_class_and_name = {
            (str((node.attributes or {}).get("class", "")), node.name): node
            for node in methods
        }
        known_node_ids = {node.id for node in out.nodes}
        call_edges = {(edge.src, edge.dst) for edge in out.edges if edge.type == EdgeType.CALLS}

        # Make the HTTP entry point part of the precise method call chain.
        for endpoint in (node for node in out.nodes if node.type == NodeType.ENDPOINT):
            endpoint_attrs = endpoint.attributes or {}
            handler = methods_by_class_and_name.get((
                str(endpoint_attrs.get("controller", "")),
                str(endpoint_attrs.get("handler", "")),
            ))
            if handler and (endpoint.id, handler.id) not in call_edges:
                out.edges.append(
                    GraphEdge(src=endpoint.id, dst=handler.id, type=EdgeType.CALLS,
                              attributes={"via": "endpoint_handler", "seq": 1})
                )
                call_edges.add((endpoint.id, handler.id))

        for caller in list(methods):
            attrs = caller.attributes or {}
            caller_class = str(attrs.get("class", ""))
            injected_fields = attrs.get("injected_fields", {})
            if not isinstance(injected_fields, dict):
                injected_fields = {}

            for expression in attrs.get("called_methods", []):
                target_class, target_method = self._resolve_method_call(
                    str(expression), caller_class, injected_fields
                )
                if not target_class or not target_method:
                    continue
                target = methods_by_class_and_name.get((target_class, target_method))
                if target is None and self._is_repository_type(target_class):
                    target = self._add_inferred_repository_method(
                        repo, target_class, target_method, out, known_node_ids
                    )
                    if target is not None:
                        methods_by_class_and_name[(target_class, target_method)] = target
                if target is None:
                    continue
                if (caller.id, target.id) in call_edges or caller.id == target.id:
                    continue
                out.edges.append(
                    GraphEdge(
                        src=caller.id,
                        dst=target.id,
                        type=EdgeType.CALLS,
                        attributes={
                            "via": "method_invocation",
                            "call_expression": expression,
                            "resolved_class": target_class,
                            "resolved_method": target_method,
                        },
                    )
                )
                call_edges.add((caller.id, target.id))

    @staticmethod
    def _resolve_method_call(
        expression: str, caller_class: str, injected_fields: dict[str, str]
    ) -> tuple[str | None, str | None]:
        if not expression:
            return None, None
        if "." not in expression:
            return caller_class, expression
        receiver, method_name = expression.rsplit(".", 1)
        receiver = receiver.removeprefix("this.").split(".", 1)[0]
        target_class = injected_fields.get(receiver)
        return (str(target_class).split("<", 1)[0].strip(), method_name) if target_class else (None, None)

    @staticmethod
    def _is_repository_type(class_name: str) -> bool:
        return class_name.endswith(("Repository", "Repo"))

    def _add_inferred_repository_method(
        self, repo: str, class_name: str, method_name: str, out: ParseResult, known_node_ids: set[str]
    ) -> GraphNode | None:
        repository_id = make_node_id("repository", repo, class_name)
        if repository_id not in known_node_ids:
            return None
        method_id = make_node_id("method", repo, f"{class_name}.{method_name}")
        existing = next((node for node in out.nodes if node.id == method_id), None)
        if existing:
            return existing
        method = GraphNode(
            id=method_id,
            type=NodeType.METHOD,
            name=method_name,
            service=repo,
            attributes={
                "class": class_name,
                "fqcn": f"{class_name}.{method_name}",
                "inferred": True,
                "inferred_reason": "Spring Data repository invocation",
                "file": next(
                    (node.attributes.get("file") for node in out.nodes if node.id == repository_id), ""
                ),
            },
        )
        out.nodes.append(method)
        out.edges.append(
            GraphEdge(src=repository_id, dst=method_id, type=EdgeType.DECLARED_IN,
                      attributes={"via": "inferred_repository_method"})
        )
        known_node_ids.add(method_id)
        return method

    def _add_business_capabilities(self, repo: str, service_id: str, out: ParseResult) -> None:
        service_node = next((n for n in out.nodes if n.id == service_id), None)
        service_name = service_node.name if service_node else repo

        endpoints = [n for n in out.nodes if n.type == NodeType.ENDPOINT]
        databases = [n for n in out.nodes if n.type in {NodeType.DATABASE, NodeType.TABLE}]
        events = [n for n in out.nodes if n.type in {NodeType.TOPIC, NodeType.QUEUE}]
        flows = [n for n in out.nodes if n.type == NodeType.EXECUTION_FLOW]

        for capability, keywords in _CAPABILITY_RULES.items():
            matched_endpoints = [
                ep for ep in endpoints
                if self._matches_capability(ep.name, ep.attributes, keywords)
            ]

            matched_dbs = [
                db for db in databases
                if self._matches_capability(db.name, db.attributes, keywords)
            ]

            matched_events = [
                ev for ev in events
                if self._matches_capability(ev.name, ev.attributes, keywords)
            ]

            flow_hits = [
                fl for fl in flows
                if self._matches_capability(fl.name, fl.attributes, keywords)
            ]

            if not matched_endpoints and not matched_dbs and not matched_events and not flow_hits and not self._matches_text(service_name, keywords):
                continue

            cap_slug = capability.lower().replace(" ", "-")
            cap_id = f"capability:_shared:{cap_slug}"

            services = [service_name]
            apis = sorted({(ep.attributes or {}).get("path") or ep.name for ep in matched_endpoints})
            db_names = sorted({db.name for db in matched_dbs})
            event_names = sorted({ev.name for ev in matched_events})
            flow_summaries = sorted({str((fl.attributes or {}).get("flow_summary", "")) for fl in flow_hits if (fl.attributes or {}).get("flow_summary")})

            summary = self._capability_summary(capability, services, apis, db_names, event_names)

            out.nodes.append(
                GraphNode(
                    id=cap_id,
                    type=NodeType.BUSINESS_CAPABILITY,
                    name=capability,
                    service=None,
                    attributes={
                        "summary": summary,
                        "services": services,
                        "apis": apis,
                        "databases": db_names,
                        "events": event_names,
                        "flow_summaries": flow_summaries,
                        "keywords": list(keywords),
                    },
                )
            )

            out.edges.append(GraphEdge(src=cap_id, dst=service_id, type=EdgeType.USES, attributes={"role": "service"}))
            for ep in matched_endpoints:
                out.edges.append(GraphEdge(src=cap_id, dst=ep.id, type=EdgeType.USES, attributes={"role": "api"}))
            for db in matched_dbs:
                out.edges.append(GraphEdge(src=cap_id, dst=db.id, type=EdgeType.USES, attributes={"role": "database"}))
            for ev in matched_events:
                out.edges.append(GraphEdge(src=cap_id, dst=ev.id, type=EdgeType.USES, attributes={"role": "event"}))
            for fl in flow_hits:
                out.edges.append(GraphEdge(src=cap_id, dst=fl.id, type=EdgeType.DEPENDS_ON, attributes={"role": "flow"}))

    @staticmethod
    def _matches_text(text: str, keywords: tuple[str, ...]) -> bool:
        hay = text.lower().replace("_", "-")
        return any(k in hay for k in keywords)

    def _matches_capability(self, name: str, attrs: dict | None, keywords: tuple[str, ...]) -> bool:
        hay = f"{name} {attrs or {}}".lower().replace("_", "-")
        return any(k in hay for k in keywords)

    @staticmethod
    def _capability_summary(
        capability: str,
        services: list[str],
        apis: list[str],
        databases: list[str],
        events: list[str],
    ) -> str:
        return (
            f"{capability} is implemented by services [{', '.join(sorted(set(services)))}], "
            f"APIs [{', '.join(apis[:8]) or 'none'}], "
            f"databases [{', '.join(databases[:8]) or 'none'}], "
            f"events [{', '.join(events[:8]) or 'none'}]."
        )

    def _add_execution_flows(self, repo: str, service_id: str, out: ParseResult) -> None:
        endpoints = [n for n in out.nodes if n.type == NodeType.ENDPOINT]
        if not endpoints:
            return

        dep_services = [
            self._node_name_by_id(out, e.dst)
            for e in out.edges
            if e.src == service_id and e.type in (EdgeType.CALLS, EdgeType.DEPENDS_ON)
            and self._node_type_by_id(out, e.dst) == NodeType.SERVICE
        ]
        dep_services = [s for s in dep_services if s]

        repositories = [n.name for n in out.nodes if n.type == NodeType.REPOSITORY]
        databases = [n.name for n in out.nodes if n.type == NodeType.DATABASE]

        method_nodes = [n for n in out.nodes if n.type == NodeType.METHOD]
        methods_by_fq: dict[str, GraphNode] = {}
        methods_by_name: dict[str, list[GraphNode]] = {}
        for m in method_nodes:
            fq = f"{(m.attributes or {}).get('class', '')}.{m.name}".strip(".")
            methods_by_fq[fq] = m
            methods_by_name.setdefault(m.name, []).append(m)

        repo_tables: dict[str, list[str]] = {}
        for e in out.edges:
            if e.type in (EdgeType.QUERIES, EdgeType.READS, EdgeType.WRITES) and e.src.startswith("repository:"):
                repo_name = self._node_name_by_id(out, e.src)
                table_name = self._node_name_by_id(out, e.dst)
                if repo_name and table_name:
                    repo_tables.setdefault(repo_name, []).append(table_name)

        published_topics = [
            self._node_name_by_id(out, e.dst)
            for e in out.edges
            if e.type in (EdgeType.PUBLISHES, EdgeType.PRODUCES_TO)
            and self._node_type_by_id(out, e.dst) == NodeType.TOPIC
        ]
        consumed_topics = [
            self._node_name_by_id(out, e.dst)
            for e in out.edges
            if e.type in (EdgeType.CONSUMES, EdgeType.CONSUMES_FROM)
            and self._node_type_by_id(out, e.dst) == NodeType.TOPIC
        ]
        published_queues = [
            self._node_name_by_id(out, e.dst)
            for e in out.edges
            if e.type == EdgeType.PUBLISHES and self._node_type_by_id(out, e.dst) == NodeType.QUEUE
        ]
        consumed_queues = [
            self._node_name_by_id(out, e.dst)
            for e in out.edges
            if e.type == EdgeType.CONSUMES and self._node_type_by_id(out, e.dst) == NodeType.QUEUE
        ]

        side_effects = []
        if published_topics:
            side_effects.append(f"Publishes topics: {', '.join(sorted(set(published_topics)))}")
        if published_queues:
            side_effects.append(f"Publishes queues: {', '.join(sorted(set(published_queues)))}")

        for ep in endpoints:
            attrs = ep.attributes or {}
            method = str(attrs.get("http_method", "HTTP"))
            path = str(attrs.get("path", ep.name))
            controller = str(attrs.get("controller", "Controller"))
            handler = str(attrs.get("handler", ""))
            delegates = [str(d) for d in attrs.get("delegates_to", []) if str(d)]

            execution_path = [f"{method} {path}", controller]
            start_method = methods_by_fq.get(f"{controller}.{handler}") if handler else None

            method_chain: list[str] = []
            repo_chain: list[str] = []
            table_chain: list[str] = []

            if start_method is not None:
                self._trace_method_chain(
                    start_method,
                    methods_by_fq,
                    methods_by_name,
                    method_chain,
                    repo_chain,
                    table_chain,
                    visited=set(),
                    depth=0,
                    max_depth=10,
                )
            else:
                # Fallback to earlier class-level delegation if handler method node is missing.
                method_chain.extend(delegates)

            execution_path.extend([m for m in method_chain if m not in execution_path])
            execution_path.extend([r for r in repo_chain if r not in execution_path])
            execution_path.extend([t for t in table_chain if t not in execution_path])

            if not repo_chain:
                execution_path.extend([r for r in repositories if r not in execution_path][:4])
            if not table_chain:
                execution_path.extend([d for d in databases if d not in execution_path][:2])
            execution_path.extend([s for s in dep_services if s and s not in execution_path][:4])

            entry_points = [f"{method} {path}"]
            exit_points = ["HTTP response"]
            if published_topics:
                exit_points.extend([f"Kafka topic: {t}" for t in sorted(set(published_topics))])
            if published_queues:
                exit_points.extend([f"Queue: {q}" for q in sorted(set(published_queues))])

            flow_summary = " -> ".join(execution_path)
            downstream_impacts = sorted(set(dep_services + consumed_topics + consumed_queues))
            sequenced_steps = [
                {"seq": i + 1, "name": step}
                for i, step in enumerate(execution_path)
            ]

            writes = sorted({
                self._node_name_by_id(out, e.dst)
                for e in out.edges
                if e.type == EdgeType.WRITES and self._node_name_by_id(out, e.dst)
            })
            reads = sorted({
                self._node_name_by_id(out, e.dst)
                for e in out.edges
                if e.type == EdgeType.READS and self._node_name_by_id(out, e.dst)
            })

            for repo_name in repo_chain:
                for tbl in repo_tables.get(repo_name, []):
                    if tbl not in reads and tbl not in writes:
                        reads.append(tbl)

            exec_id = f"execution_flow:{repo}:{method}:{path}"
            biz_id = f"business_flow:{repo}:{method}:{path}"
            tech_id = f"technical_flow:{repo}:{method}:{path}"

            out.nodes.append(
                GraphNode(
                    id=exec_id,
                    type=NodeType.EXECUTION_FLOW,
                    name=f"{method} {path}",
                    service=repo,
                    attributes={
                        "endpoint_id": ep.id,
                        "entry_points": entry_points,
                        "exit_points": exit_points,
                        "execution_path": execution_path,
                        "sequenced_steps": sequenced_steps,
                        "flow_summary": flow_summary,
                        "side_effects": side_effects,
                        "downstream_impacts": downstream_impacts,
                    },
                )
            )
            out.nodes.append(
                GraphNode(
                    id=biz_id,
                    type=NodeType.BUSINESS_FLOW,
                    name=f"Business flow {method} {path}",
                    service=repo,
                    attributes={
                        "endpoint_id": ep.id,
                        "flow_summary": (
                            f"{controller} orchestrates {', '.join(delegates) or 'service logic'}; "
                            f"downstream dependencies: {', '.join(dep_services) or 'none'}"
                        ),
                        "entry_points": entry_points,
                        "exit_points": exit_points,
                        "side_effects": side_effects,
                        "downstream_impacts": downstream_impacts,
                    },
                )
            )
            out.nodes.append(
                GraphNode(
                    id=tech_id,
                    type=NodeType.TECHNICAL_FLOW,
                    name=f"Technical flow {method} {path}",
                    service=repo,
                    attributes={
                        "endpoint_id": ep.id,
                        "flow_summary": (
                            f"Reads: {', '.join(reads) or 'none'}; "
                            f"Writes: {', '.join(writes) or 'none'}; "
                            f"Publishes: {', '.join(sorted(set(published_topics + published_queues))) or 'none'}"
                        ),
                        "entry_points": entry_points,
                        "exit_points": exit_points,
                        "side_effects": side_effects,
                        "downstream_impacts": downstream_impacts,
                        "reads": reads,
                        "writes": writes,
                    },
                )
            )

            out.edges.append(GraphEdge(src=exec_id, dst=ep.id, type=EdgeType.DECLARED_IN, attributes={"role": "entry"}))
            out.edges.append(GraphEdge(src=biz_id, dst=ep.id, type=EdgeType.DECLARED_IN, attributes={"role": "entry"}))
            out.edges.append(GraphEdge(src=tech_id, dst=ep.id, type=EdgeType.DECLARED_IN, attributes={"role": "entry"}))

            # Add ordered execution edges so downstream graph visualization can render exact path order.
            for idx, step in enumerate(execution_path, start=1):
                step_node_id = self._resolve_step_node_id(out, repo, step, controller)
                if not step_node_id:
                    continue
                out.edges.append(
                    GraphEdge(
                        src=exec_id,
                        dst=step_node_id,
                        type=EdgeType.CALLS,
                        attributes={"role": "execution-step", "seq": idx, "step": step},
                    )
                )

            for svc_name in dep_services:
                dst_id = _service_node_id(svc_name)
                out.edges.append(GraphEdge(src=biz_id, dst=dst_id, type=EdgeType.DEPENDS_ON, attributes={"role": "impact"}))

            for db in databases:
                out.edges.append(GraphEdge(src=tech_id, dst=_db_node_id(db), type=EdgeType.USES, attributes={"role": "storage"}))

            for topic in sorted(set(published_topics)):
                out.edges.append(GraphEdge(src=tech_id, dst=f"topic:_shared:{topic}", type=EdgeType.PUBLISHES, attributes={"role": "exit"}))
            for queue in sorted(set(published_queues)):
                out.edges.append(GraphEdge(src=tech_id, dst=_queue_node_id(queue), type=EdgeType.PUBLISHES, attributes={"role": "exit"}))

    @staticmethod
    def _node_name_by_id(out: ParseResult, node_id: str) -> str:
        node = next((n for n in out.nodes if n.id == node_id), None)
        return node.name if node else ""

    @staticmethod
    def _node_type_by_id(out: ParseResult, node_id: str) -> NodeType | None:
        node = next((n for n in out.nodes if n.id == node_id), None)
        return node.type if node else None

    def _trace_method_chain(
        self,
        method_node: GraphNode,
        methods_by_fq: dict[str, GraphNode],
        methods_by_name: dict[str, list[GraphNode]],
        method_chain: list[str],
        repo_chain: list[str],
        table_chain: list[str],
        *,
        visited: set[str],
        depth: int,
        max_depth: int,
    ) -> None:
        if depth > max_depth:
            return
        attrs = method_node.attributes or {}
        class_name = str(attrs.get("class", ""))
        fq = f"{class_name}.{method_node.name}".strip(".")
        if fq in visited:
            return
        visited.add(fq)

        if fq and fq not in method_chain:
            method_chain.append(fq)

        for repo_name in [str(r) for r in attrs.get("delegates_to_repos", []) if str(r)]:
            if repo_name not in repo_chain:
                repo_chain.append(repo_name)

        for called in [str(c) for c in attrs.get("called_methods", []) if str(c)]:
            target = self._resolve_called_method(called, methods_by_fq, methods_by_name)
            if target is not None:
                self._trace_method_chain(
                    target,
                    methods_by_fq,
                    methods_by_name,
                    method_chain,
                    repo_chain,
                    table_chain,
                    visited=visited,
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            else:
                # External/service call not resolved locally; keep it as a flow step.
                if "." in called and called not in method_chain:
                    method_chain.append(called)

        for table_hint in [str(t) for t in attrs.get("database_tables_used", []) if str(t)]:
            if table_hint not in table_chain:
                table_chain.append(table_hint)

    @staticmethod
    def _resolve_called_method(
        called: str,
        methods_by_fq: dict[str, GraphNode],
        methods_by_name: dict[str, list[GraphNode]],
    ) -> GraphNode | None:
        if called in methods_by_fq:
            return methods_by_fq[called]

        if "." in called:
            obj, method = called.split(".", 1)
            obj_l = obj.lower()
            candidates = methods_by_name.get(method, [])
            for c in candidates:
                cls = str((c.attributes or {}).get("class", ""))
                cls_l = cls.lower()
                if obj_l and (obj_l in cls_l or cls_l.startswith(obj_l)):
                    return c

        short = called.split(".")[-1]
        candidates = methods_by_name.get(short, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _resolve_step_node_id(out: ParseResult, repo: str, step: str, controller: str) -> str | None:
        step_clean = step.strip()
        if not step_clean:
            return None

        if step_clean.startswith(("GET ", "POST ", "PUT ", "DELETE ", "PATCH ", "HTTP ")):
            return None

        if "." in step_clean:
            cls_name = step_clean.split(".", 1)[0]
            method_name = step_clean.split(".", 1)[1]
            for n in out.nodes:
                if n.type == NodeType.METHOD:
                    attrs = n.attributes or {}
                    if attrs.get("class") == cls_name and n.name == method_name:
                        return n.id

        for n in out.nodes:
            if n.name == step_clean and n.type in {
                NodeType.CONTROLLER,
                NodeType.SERVICE_LAYER,
                NodeType.REPOSITORY,
                NodeType.SERVICE,
                NodeType.TABLE,
                NodeType.DATABASE,
            }:
                return n.id

        if step_clean == controller:
            return f"controller:{repo}:{controller}"

        return None

    def _ensure_gateway_node(self, repo: str, service_id: str, out: ParseResult) -> None:
        lower = repo.lower()
        if "gateway" not in lower:
            return
        node_id = _gateway_node_id(repo)
        out.nodes.append(
            GraphNode(
                id=node_id,
                type=NodeType.GATEWAY,
                name=repo,
                service=repo,
                attributes={"role": "api-gateway", "source": "repo-name-heuristic"},
            )
        )
        out.edges.append(
            GraphEdge(src=node_id, dst=service_id, type=EdgeType.CALLS, attributes={"via": "gateway"})
        )

    def _add_http_and_feign_calls(
        self,
        repo: str,
        service_id: str,
        out: ParseResult,
        repo_names: set[str],
    ) -> None:
        known_call_pairs = {
            (e.src, e.dst) for e in out.edges if e.type == EdgeType.CALLS and e.src.startswith("service:")
        }
        for node in out.nodes:
            if node.type != NodeType.METHOD:
                continue
            attrs = node.attributes or {}
            body = str(attrs.get("body", ""))
            called_methods = [str(c) for c in attrs.get("called_methods", [])]

            inferred_targets: set[str] = set()
            for host in _HOST_RE.findall(body):
                host_norm = host.lower().split(":")[0]
                token = host_norm.split(".")[0].replace("_", "-")
                inferred_targets.add(token)

            if any(any(k in cm.lower() for k in ("resttemplate", "webclient", "feign", "httpclient")) for cm in called_methods):
                for host in _HOST_RE.findall(body):
                    token = host.lower().split(".")[0].replace("_", "-")
                    inferred_targets.add(token)

            for target in sorted(inferred_targets):
                resolved = self._resolve_repo_name(target, repo_names)
                if not resolved or resolved == repo:
                    continue
                dst_id = _service_node_id(resolved)
                if (service_id, dst_id) in known_call_pairs:
                    continue
                known_call_pairs.add((service_id, dst_id))
                out.nodes.append(
                    GraphNode(
                        id=dst_id,
                        type=NodeType.SERVICE,
                        name=resolved,
                        service=resolved,
                        attributes={"source": "http-call-inference"},
                    )
                )
                out.edges.append(
                    GraphEdge(
                        src=service_id,
                        dst=dst_id,
                        type=EdgeType.CALLS,
                        attributes={"via": "http-client", "from_method": attrs.get("class", "") + "." + node.name},
                    )
                )

    def _add_kafka_alias_edges(self, out: ParseResult) -> None:
        for edge in list(out.edges):
            if edge.type == EdgeType.PRODUCES_TO:
                out.edges.append(GraphEdge(src=edge.src, dst=edge.dst, type=EdgeType.PUBLISHES, attributes=edge.attributes))
            elif edge.type == EdgeType.CONSUMES_FROM:
                out.edges.append(GraphEdge(src=edge.src, dst=edge.dst, type=EdgeType.CONSUMES, attributes=edge.attributes))

    def _add_shared_lib_dependencies(
        self,
        repo: str,
        service_id: str,
        out: ParseResult,
        repo_names: set[str],
    ) -> None:
        for node in out.nodes:
            if node.type != NodeType.DEPENDENCY:
                continue
            dep = (node.attributes or {}).get("artifactId") or node.name
            dep_l = str(dep).lower()
            if not any(k in dep_l for k in ("lib", "common", "shared", "starter")):
                continue

            out.edges.append(
                GraphEdge(src=service_id, dst=node.id, type=EdgeType.USES, attributes={"kind": "shared-library"})
            )

            resolved = self._resolve_repo_name(dep_l, repo_names)
            if resolved and resolved != repo:
                dst_id = _service_node_id(resolved)
                out.nodes.append(
                    GraphNode(
                        id=dst_id,
                        type=NodeType.SERVICE,
                        name=resolved,
                        service=resolved,
                        attributes={"source": "shared-library-resolution"},
                    )
                )
                out.edges.append(
                    GraphEdge(src=service_id, dst=dst_id, type=EdgeType.DEPENDS_ON, attributes={"via": "shared-library", "dependency": dep})
                )

    def _add_database_dependencies(self, repo: str, service_id: str, out: ParseResult) -> None:
        for node in out.nodes:
            if node.type != NodeType.CONFIG:
                continue
            ds = (node.attributes or {}).get("datasource_url")
            if not isinstance(ds, str) or not ds:
                continue
            db_name = self._database_name_from_url(ds) or f"{repo}-db"
            db_id = _db_node_id(db_name)
            out.nodes.append(
                GraphNode(
                    id=db_id,
                    type=NodeType.DATABASE,
                    name=db_name,
                    service=None,
                    attributes={"datasource_url": ds},
                )
            )
            out.edges.append(
                GraphEdge(src=service_id, dst=db_id, type=EdgeType.USES, attributes={"via": "datasource"})
            )

    def _add_queue_dependencies(self, repo: str, service_id: str, out: ParseResult) -> None:
        for node in out.nodes:
            if node.type != NodeType.CONFIG_PROPERTY:
                continue
            key = str((node.attributes or {}).get("name") or node.name).lower()
            val = str((node.attributes or {}).get("value") or "")
            if "queue" not in key and "rabbit" not in key:
                continue
            queue_name = val if val and val != "<redacted>" else node.name.split(".")[-1]
            queue_name = queue_name.strip()
            if not queue_name:
                continue
            q_id = _queue_node_id(queue_name)
            out.nodes.append(
                GraphNode(id=q_id, type=NodeType.QUEUE, name=queue_name, service=None, attributes={"source": "config"})
            )
            if any(k in key for k in ("producer", "publish", "out", "send")):
                out.edges.append(GraphEdge(src=service_id, dst=q_id, type=EdgeType.PUBLISHES, attributes={"via": "queue-config"}))
            if any(k in key for k in ("consumer", "listen", "in", "receive")):
                out.edges.append(GraphEdge(src=service_id, dst=q_id, type=EdgeType.CONSUMES, attributes={"via": "queue-config"}))

    def _add_repository_table_read_write_edges(self, out: ParseResult) -> None:
        query_by_repo: dict[str, list[str]] = {}
        for node in out.nodes:
            if node.type != NodeType.REPOSITORY:
                continue
            queries = []
            for qm in (node.attributes or {}).get("query_methods", []):
                q = qm.get("query")
                if isinstance(q, str) and q.strip():
                    queries.append(q)
            query_by_repo[node.id] = queries

        for edge in list(out.edges):
            if edge.type != EdgeType.QUERIES:
                continue
            repo_id = edge.src
            table_or_entity_id = edge.dst
            for query in query_by_repo.get(repo_id, []):
                if _SQL_WRITE.search(query):
                    out.edges.append(GraphEdge(src=repo_id, dst=table_or_entity_id, type=EdgeType.WRITES, attributes={"source": "query"}))
                elif _SQL_READ.search(query):
                    out.edges.append(GraphEdge(src=repo_id, dst=table_or_entity_id, type=EdgeType.READS, attributes={"source": "query"}))

        for node in out.nodes:
            if node.type != NodeType.METHOD:
                continue
            delegated = [str(r) for r in (node.attributes or {}).get("delegates_to_repos", [])]
            called = " ".join(str(c) for c in (node.attributes or {}).get("called_methods", []))
            rw_type = EdgeType.WRITES if _SQL_WRITE.search(called) else EdgeType.READS
            for repo_name in delegated:
                repo_node_id = next((n.id for n in out.nodes if n.type == NodeType.REPOSITORY and n.name == repo_name), None)
                if not repo_node_id:
                    continue
                for edge in out.edges:
                    if edge.src == repo_node_id and edge.type in (EdgeType.QUERIES, EdgeType.READS, EdgeType.WRITES):
                        out.edges.append(
                            GraphEdge(src=node.id, dst=edge.dst, type=rw_type, attributes={"via": repo_name})
                        )

    @staticmethod
    def _database_name_from_url(url: str) -> str | None:
        m = _DB_URL_RE.match(url)
        if not m:
            return None
        host = m.group(1) or "db"
        db = m.group(2) or "default"
        return f"{host}/{db}".lower()

    @staticmethod
    def _resolve_repo_name(token: str, repo_names: set[str]) -> str | None:
        t = token.lower().replace("_", "-")
        for name in repo_names:
            n = name.lower()
            if t == n:
                return name
            if t in n or n in t:
                return name
        return None

    def _local_repo_names(self) -> set[str]:
        root = Path(self._settings.repos_root)
        if not root.exists():
            return set()
        return {p.name for p in root.iterdir() if p.is_dir()}
