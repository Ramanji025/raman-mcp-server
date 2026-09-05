"""KnowledgeService mixin: CodeIntelMixin (code_intel domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)


from ._helpers import _infer_method_purpose


class CodeIntelMixin:
    """Code-level intelligence tools: call graphs, method detail, code search."""
    def get_method_detail(self, class_name: str, method_name: str) -> ToolResponse:
        """Full method body, parameters, local variables, call chain — for targeted fixes."""
        # Search by method name and class in graph.
        all_methods = self.graph.nodes_by_type(NodeType.METHOD)
        matches = [m for m in all_methods
                   if m.get("name") == method_name and
                   m.get("attributes", {}).get("class", "").lower() == class_name.lower()]
        if not matches:
            # Fuzzy: just method name.
            matches = [m for m in all_methods if m.get("name") == method_name]
        if not matches:
            # Fuzzy: method name contains.
            matches = [m for m in all_methods if method_name.lower() in m.get("name", "").lower()]

        if not matches:
            # Fallback to vector search.
            retrieval = self.retriever.retrieve(
                f"method {method_name} in class {class_name}",
                collections=("code",), top_k=5,
            )
            md_lines = [f"# Method: `{class_name}.{method_name}`",
                        "",
                        "No method node found in graph. Closest code snippets from vector search:", ""]
            for rc in retrieval.chunks:
                md_lines.append(f"**`{rc.chunk.rel_path}` (line {rc.chunk.start_line})**")
                md_lines.append(f"```java\n{rc.chunk.text[:2000]}\n```")
            return ToolResponse(
                tool="get_method_detail",
                query={"class_name": class_name, "method_name": method_name},
                summary=f"Method node not found; {len(retrieval.chunks)} related code chunks returned.",
                data={"found": False, "vector_results": len(retrieval.chunks)},
                markdown="\n".join(md_lines),
                citations=citations_from(retrieval.chunks),
            )

        data_methods = []
        for m in matches[:5]:
            attrs = m.get("attributes", {})
            data_methods.append({
                "class": attrs.get("class"),
                "method": m.get("name"),
                "service": m.get("service"),
                "file": attrs.get("file"),
                "lines": f"{attrs.get('start_line')}-{attrs.get('end_line')}",
                "signature": self._build_signature(attrs, m.get("name", "")),
                "parameters": attrs.get("parameters", []),
                "returns": attrs.get("returns"),
                "annotations": attrs.get("annotations", []),
                "transactional": attrs.get("transactional"),
                "thrown_exceptions": attrs.get("thrown_exceptions", []),
                "called_methods": attrs.get("called_methods", []),
                "local_variables": attrs.get("local_variables", []),
                "line_count": attrs.get("line_count"),
                "body": attrs.get("body", ""),
            })

        data = {"matches": len(data_methods), "methods": data_methods}
        md = self._render_method_detail_md(class_name, method_name, data_methods)
        return ToolResponse(
            tool="get_method_detail",
            query={"class_name": class_name, "method_name": method_name},
            summary=(f"Found {len(data_methods)} match(es) for "
                     f"`{class_name}.{method_name}`."),
            data=data, markdown=md,
        )
    @staticmethod
    def _render_method_detail_md(class_name: str, method_name: str,
                                 methods: list[dict]) -> str:
        lines = [f"# Method Detail: `{class_name}.{method_name}`", ""]
        for m in methods:
            lines += [
                f"## `{m['class']}.{m['method']}`",
                f"**Service**: `{m['service']}` | **File**: `{m['file']}`",
                f"**Lines**: {m['lines']} | **Size**: {m.get('line_count', '?')} lines",
                "",
                "### Signature",
                f"```java\n{m['signature']}\n```",
                "",
            ]
            if m["parameters"]:
                lines.append("### Parameters")
                for p in m["parameters"]:
                    ann = f" `{p['annotations']}`" if p.get("annotations") else ""
                    lines.append(f"- `{p['type']} {p['name']}`{ann}")
                lines.append("")
            if m["annotations"]:
                lines.append(f"### Annotations\n{', '.join(f'`@{a}`' for a in m['annotations'])}\n")
            if m.get("transactional"):
                lines.append("**@Transactional**: Yes\n")
            if m["thrown_exceptions"]:
                lines.append("### Throws\n" +
                             "\n".join(f"- `{e}`" for e in m["thrown_exceptions"]) + "\n")
            if m["called_methods"]:
                lines.append("### Calls")
                for call in m["called_methods"][:15]:
                    lines.append(f"- `{call}`")
                lines.append("")
            if m["local_variables"]:
                lines.append("### Local Variables")
                for lv in m["local_variables"][:10]:
                    lines.append(f"- `{lv['type']} {lv['name']}`")
                lines.append("")
            if m.get("body"):
                lines += ["### Full Method Body",
                          f"```java\n{m['body']}\n```", ""]
        return "\n".join(lines)
    @staticmethod
    def _build_signature(attrs: dict, method_name: str) -> str:
        params = attrs.get("parameters", [])
        param_str = ", ".join(p.get("type", "") + " " + p.get("name", "") for p in params)
        return f"{attrs.get('returns', 'void')} {method_name}({param_str})"
    def explain_code(self, file_path: str,
                     start_line: int | None = None,
                     end_line: int | None = None) -> ToolResponse:
        """Explain code in a file at given lines using the pre-built knowledge graph."""
        nodes = self.graph.find_by_file_lines(file_path, start_line, end_line)

        methods   = [n for n in nodes if n.get("type") == "Method"]
        endpoints = [n for n in nodes if n.get("type") == "Endpoint"]
        classes   = [n for n in nodes if n.get("type") in
                     ("Controller", "ServiceLayer", "Repository", "Entity", "DTO")]

        file_basename = file_path.split("/")[-1].split("\\")[-1].replace(".java", "")
        line_hint = f"lines {start_line} to {end_line}" if start_line else ""
        retrieval = self.retriever.retrieve(
            f"{file_basename} {line_hint} implementation logic",
            collections=("code",), top_k=8,
        )
        file_chunks = [rc for rc in retrieval.chunks
                       if file_basename.lower() in rc.chunk.rel_path.lower()]
        if start_line and end_line:
            file_chunks = [rc for rc in file_chunks
                           if rc.chunk.start_line is not None and
                           rc.chunk.end_line is not None and
                           not (rc.chunk.end_line < start_line or
                                rc.chunk.start_line > end_line)]

        services = sorted({n.get("service") for n in nodes if n.get("service")})
        data = {
            "file": file_path,
            "lines": f"{start_line}-{end_line}" if start_line else "all",
            "services": services,
            "total_nodes_found": len(nodes),
            "class_context": [self._node_summary(n) for n in classes],
            "methods_in_range": [self._method_explanation(n) for n in methods],
            "endpoints_in_range": [self._endpoint_explanation(n) for n in endpoints],
            "code_snippets": [
                {"file": rc.chunk.rel_path,
                 "lines": f"{rc.chunk.start_line}-{rc.chunk.end_line}",
                 "code": rc.chunk.text}
                for rc in file_chunks[:5]
            ],
        }
        md = self._render_explain_code_md(file_path, start_line, end_line, data)
        return ToolResponse(
            tool="explain_code",
            query={"file_path": file_path, "start_line": start_line, "end_line": end_line},
            summary=f"Explained {len(nodes)} nodes in `{file_path}` ({line_hint or 'all lines'}).",
            data=data, markdown=md, citations=citations_from(file_chunks),
        )
    @staticmethod
    def _node_summary(n: dict) -> dict:
        attrs = n.get("attributes", {})
        return {
            "name": n.get("name"), "type": n.get("type"),
            "service": n.get("service"), "file": attrs.get("file"),
            "lines": f"{attrs.get('start_line', '?')}-{attrs.get('end_line', '?')}",
            "fqcn": attrs.get("fqcn"), "kind": attrs.get("kind"),
        }
    @staticmethod
    def _method_explanation(n: dict) -> dict:
        attrs = n.get("attributes", {})
        params = attrs.get("parameters", [])
        param_str = ", ".join(f"{p.get('type','?')} {p.get('name','?')}" for p in params)
        return {
            "method": n.get("name"),
            "class": attrs.get("class"),
            "signature": f"{attrs.get('returns','void')} {n.get('name')}({param_str})",
            "lines": f"{attrs.get('start_line','?')}-{attrs.get('end_line','?')}",
            "line_count": attrs.get("line_count"),
            "annotations": attrs.get("annotations", []),
            "transactional": attrs.get("transactional"),
            "thrown_exceptions": attrs.get("thrown_exceptions", []),
            "called_methods": attrs.get("called_methods", []),
            "local_variables": attrs.get("local_variables", []),
            "body": attrs.get("body", ""),
            "purpose": _infer_method_purpose(
                n.get("name", ""), attrs.get("called_methods", []),
                attrs.get("thrown_exceptions", []), attrs.get("transactional", False),
            ),
        }
    @staticmethod
    def _endpoint_explanation(n: dict) -> dict:
        attrs = n.get("attributes", {})
        return {
            "endpoint": n.get("name"),
            "http_method": attrs.get("http_method"),
            "path": attrs.get("path"),
            "controller": attrs.get("controller"),
            "handler": attrs.get("handler"),
            "security": attrs.get("security"),
            "request_body": attrs.get("request_body"),
            "returns": attrs.get("returns"),
            "thrown_exceptions": attrs.get("thrown_exceptions", []),
            "transactional": attrs.get("transactional"),
        }
    @staticmethod
    def _render_explain_code_md(file: str, s: int | None, e: int | None, d: dict) -> str:
        line_range = f" (lines {s}–{e})" if s else ""
        lines = [f"# Code Explanation: `{file}`{line_range}", ""]

        if d["class_context"]:
            lines.append("## Class Context")
            for c in d["class_context"]:
                lines += [f"### `{c['name']}` [{c['type']}]",
                          f"**FQCN**: `{c.get('fqcn', '?')}` | "
                          f"**Service**: `{c['service']}` | **Lines**: {c['lines']}", ""]

        if d["endpoints_in_range"]:
            lines.append("## REST Endpoints in Range")
            for ep in d["endpoints_in_range"]:
                auth = f" 🔒 `{ep['security']}`" if ep.get("security") else " 🔓 no auth"
                lines += [f"### `{ep['http_method']} {ep['path']}`{auth}",
                          f"- Handler: `{ep['controller']}.{ep['handler']}`"]
                if ep.get("request_body"):
                    lines.append(f"- Request body: `{ep['request_body']}`")
                if ep.get("returns"):
                    lines.append(f"- Returns: `{ep['returns']}`")
                if ep.get("thrown_exceptions"):
                    lines.append(f"- Throws: {', '.join(f'`{t}`' for t in ep['thrown_exceptions'])}")
                lines.append("")

        if d["methods_in_range"]:
            lines.append("## Methods in Range")
            for m in d["methods_in_range"]:
                lines += [f"### `{m['class']}.{m['method']}` — lines {m['lines']}",
                          f"**Signature**: `{m['signature']}`"]
                if m.get("purpose"):
                    lines.append(f"**Purpose**: {m['purpose']}")
                if m.get("annotations"):
                    lines.append("**Annotations**: " +
                                 ", ".join(f"`@{a}`" for a in m["annotations"]))
                if m.get("transactional"):
                    lines.append("**@Transactional**: Yes")
                if m.get("called_methods"):
                    lines.append("**Calls**: " +
                                 ", ".join(f"`{c}`" for c in m["called_methods"][:8]))
                if m.get("thrown_exceptions"):
                    lines.append("**Throws**: " +
                                 ", ".join(f"`{t}`" for t in m["thrown_exceptions"]))
                if m.get("local_variables"):
                    lines.append("**Local vars**: " +
                                 ", ".join(f"`{v['type']} {v['name']}`"
                                           for v in m["local_variables"][:6]))
                if m.get("body"):
                    lines += [f"```java\n{m['body'][:1500]}\n```"]
                lines.append("")

        if d["code_snippets"]:
            lines += ["## Vector-Matched Code Snippets"]
            for cs in d["code_snippets"]:
                lines += [f"**`{cs['file']}` lines {cs['lines']}**",
                          f"```java\n{cs['code'][:1000]}\n```", ""]
        return "\n".join(lines)
    def trace_call_chain(self, class_name: str, method_name: str) -> ToolResponse:
        """Complete execution trace: who calls this → what it calls → all the way to DB."""
        all_nodes_data = self.graph.all_nodes()
        start_nodes = [
            nd for nd in all_nodes_data
            if nd.get("name") == method_name and
            nd.get("attributes", {}).get("class", "").lower() == class_name.lower()
        ]
        if not start_nodes:
            start_nodes = [nd for nd in all_nodes_data
                           if nd.get("name") == method_name and
                           nd.get("type") in ("Method", "Endpoint")]
        if not start_nodes:
            return self._not_found("trace_call_chain",
                                   {"class_name": class_name, "method_name": method_name},
                                   f"{class_name}.{method_name}")

        target = start_nodes[0]
        attrs = target.get("attributes", {})

        # Inbound: endpoints that HTTP-route to this class.
        cls_node_id = next(
            (nd["id"] for nd in all_nodes_data
             if nd.get("name") == class_name and
             nd.get("type") in ("ServiceLayer", "Controller", "Repository")),
            None,
        )
        entry_endpoints: list[dict] = []
        if cls_node_id:
            for relation in self.graph.neighbors(cls_node_id, direction="in"):
                src = relation.get("node")
                if src and src.get("type") in ("Endpoint", "Controller"):
                    entry_endpoints.append(self._endpoint_explanation(src))

        # Outbound: resolve called_methods to METHOD nodes.
        called = attrs.get("called_methods", [])
        delegates = attrs.get("delegates_to_repos", [])
        outbound_nodes: list[dict] = []
        for call_str in called[:20]:
            m_name_part = call_str.split(".")[-1]
            found = [nd for nd in all_nodes_data
                     if nd.get("name") == m_name_part and nd.get("type") == "Method"]
            for fn in found[:1]:
                fn_attrs = fn.get("attributes", {})
                outbound_nodes.append({
                    "call": call_str,
                    "resolved_class": fn_attrs.get("class"),
                    "service": fn.get("service"),
                    "transactional": fn_attrs.get("transactional"),
                    "returns": fn_attrs.get("returns"),
                    "body_preview": fn_attrs.get("body", "")[:300],
                })

        # DB layer.
        db_layer: list[dict] = []
        for repo_type in delegates:
            repo_nodes = [nd for _, nd in all_nodes_data
                          if nd.get("name") == repo_type and nd.get("type") == "Repository"]
            for rn in repo_nodes[:1]:
                qm = rn.get("attributes", {}).get("query_methods", [])
                db_layer.append({
                    "repository": repo_type,
                    "managed_entity": rn.get("attributes", {}).get("managed_entity", ""),
                    "query_methods": [q.get("name") for q in qm[:8]],
                })

        data = {
            "target": {
                "class": class_name, "method": method_name,
                "signature": self._build_signature(attrs, method_name),
                "file": attrs.get("file"),
                "lines": f"{attrs.get('start_line')}-{attrs.get('end_line')}",
                "body": attrs.get("body", "")[:2000],
                "transactional": attrs.get("transactional"),
            },
            "entry_points": entry_endpoints,
            "outbound_calls": outbound_nodes,
            "database_layer": db_layer,
            "thrown_exceptions": attrs.get("thrown_exceptions", []),
        }
        md = self._render_call_chain_md(class_name, method_name, data)
        return ToolResponse(
            tool="trace_call_chain",
            query={"class_name": class_name, "method_name": method_name},
            summary=(f"Call chain for `{class_name}.{method_name}`: "
                     f"{len(entry_endpoints)} HTTP entry point(s), "
                     f"{len(outbound_nodes)} outbound call(s), "
                     f"{len(db_layer)} DB layer(s)."),
            data=data, markdown=md,
        )
    def _render_call_chain_md(cls: str, method: str, d: dict) -> str:
        t = d["target"]
        lines = [f"# Call Chain: `{cls}.{method}`",
                 f"**File**: `{t['file']}` | **Lines**: {t['lines']}",
                 f"**Signature**: `{t['signature']}`",
                 f"**@Transactional**: {t.get('transactional', False)}", ""]
        if d["entry_points"]:
            lines += ["## HTTP Entry Points (routes to this class)"]
            for ep in d["entry_points"]:
                lines.append(f"- `{ep.get('http_method')} {ep.get('path')}` → "
                              f"`{ep.get('controller')}.{ep.get('handler')}`")
            lines.append("")
        lines += ["## Method Body", f"```java\n{t['body']}\n```", ""]
        if d["outbound_calls"]:
            lines += ["## Outbound Calls"]
            for c in d["outbound_calls"]:
                txn = " [@Tx]" if c.get("transactional") else ""
                lines.append(f"- `{c['call']}` → `{c['resolved_class']}`{txn} "
                              f"[{c['service']}] → `{c['returns']}`")
                if c.get("body_preview"):
                    lines += [f"  ```java\n  {c['body_preview'][:200]}\n  ```"]
            lines.append("")
        if d["database_layer"]:
            lines += ["## Database Layer"]
            for db in d["database_layer"]:
                lines.append(f"- **`{db['repository']}`** → entity `{db['managed_entity']}`")
                if db["query_methods"]:
                    lines.append(f"  Query methods: {', '.join(f'`{q}`' for q in db['query_methods'])}")
            lines.append("")
        if d["thrown_exceptions"]:
            lines += ["## Exceptions", ", ".join(f"`{e}`" for e in d["thrown_exceptions"])]
        return "\n".join(lines)
    def find_similar_code(self, description: str,
                          service_name: str | None = None) -> ToolResponse:
        """Find all code across services that implements similar logic."""
        retrieval = self.retriever.retrieve(
            description, collections=("code",), top_k=15,
            service=service_name if service_name else None,
        )
        groups: dict[str, list[dict]] = {}
        for rc in retrieval.chunks:
            key = (f"{rc.chunk.metadata.get('service', rc.chunk.repo)}/"
                   f"{rc.chunk.metadata.get('class', '')}")
            groups.setdefault(key, []).append({
                "service": rc.chunk.metadata.get("service", rc.chunk.repo),
                "class": rc.chunk.metadata.get("class", ""),
                "method": rc.chunk.metadata.get("method", ""),
                "file": rc.chunk.rel_path,
                "lines": f"{rc.chunk.start_line}-{rc.chunk.end_line}",
                "score": round(rc.score, 3),
                "snippet": rc.chunk.text[:400],
            })
        results = sorted(
            [{"location": k, "matches": v,
              "max_score": max(m["score"] for m in v)}
             for k, v in groups.items()],
            key=lambda x: -x["max_score"],
        )
        data = {"description": description, "groups_found": len(results),
                "total_matches": len(retrieval.chunks), "results": results}
        lines = [f"# Similar Code: _{description}_",
                 f"**{len(results)} location(s)**", ""]
        for r in results[:10]:
            m = r["matches"][0]
            lines += [f"## `{m['service']}/{m['class']}` (score: {r['max_score']})",
                      f"**File**: `{m['file']}` | **Lines**: {m['lines']}"]
            if m["method"]:
                lines.append(f"**Method**: `{m['method']}`")
            lines += [f"```java\n{m['snippet']}\n```", ""]
        return ToolResponse(
            tool="find_similar_code", query={"description": description},
            summary=f"Found {len(results)} similar implementation(s) for: '{description}'.",
            data=data, markdown="\n".join(lines),
            citations=citations_from(retrieval.chunks),
        )
    def list_indexed_files(self, service_name: str | None = None) -> ToolResponse:
        """List all files in the knowledge base with node counts — so users know what's indexed."""
        file_index = self.graph.all_files()
        result: list[dict] = []
        svc_filter = self._resolve_service(service_name) if service_name else None
        for file_path, node_ids in sorted(file_index.items()):
            nodes = [self.graph.node(nid) for nid in node_ids if self.graph.node(nid)]
            svc = nodes[0].get("service") if nodes else None
            if svc_filter and svc != svc_filter:
                continue
            types_count: dict[str, int] = {}
            for n in nodes:
                t = n.get("type", "?")
                types_count[t] = types_count.get(t, 0) + 1
            result.append({"file": file_path, "service": svc,
                           "node_count": len(nodes), "types": types_count})
        data = {"total_files": len(result), "files": result}
        lines = [f"# Indexed Files ({len(result)} total)", "",
                 "Use `explain_code(file_path, start_line, end_line)` on any file below.", ""]
        for f in result:
            type_summary = ", ".join(f"{v}×{k}" for k, v in sorted(f["types"].items()))
            lines.append(f"- `{f['file']}` [{f['service']}] — {type_summary}")
        return ToolResponse(
            tool="list_indexed_files", query={"service_name": service_name},
            summary=f"{len(result)} files indexed.",
            data=data, markdown="\n".join(lines),
        )
    def find_callers(self, class_name: str, method_name: str) -> ToolResponse:
        """All call sites (endpoints, service methods, repositories) that reach
        `class_name.method_name`, e.g. "Find all callers of processPayment()"."""
        node_id = self._resolve_method_node(class_name, method_name)
        if node_id is None:
            return self._not_found("find_callers",
                                   {"class_name": class_name, "method_name": method_name},
                                   f"{class_name}.{method_name}")
        callers = self._find_callers(node_id)
        total = (len(callers["endpoints"]) + len(callers["service_methods"])
                 + len(callers["repositories"]))
        data = {"target": {"class": class_name, "method": method_name, "node_id": node_id},
                **callers, "total_callers": total}
        lines = [f"# Callers of `{class_name}.{method_name}`", "",
                 f"**{total} caller(s) found.**", ""]
        if callers["endpoints"]:
            lines.append("## HTTP entry points")
            for ep in callers["endpoints"]:
                lines.append(f"- `{ep.get('http_method', '')} {ep.get('path', '')}` "
                             f"({ep.get('controller', '')}) [{ep.get('service')}]")
        if callers["service_methods"]:
            lines.append("## Calling service methods")
            for sm in callers["service_methods"]:
                lines.append(f"- `{sm.get('class')}.{sm.get('name')}` [{sm.get('service')}]")
        if callers["repositories"]:
            lines.append("## Calling repositories")
            for r in callers["repositories"]:
                lines.append(f"- `{r.get('name')}` [{r.get('service')}]")
        if not total:
            lines.append("_No callers found in the indexed graph — this may be a public "
                         "entry point, or the graph needs a re-index._")
        return ToolResponse(
            tool="find_callers", query={"class_name": class_name, "method_name": method_name},
            summary=f"{total} caller(s) of `{class_name}.{method_name}`.",
            data=data, markdown="\n".join(lines),
        )
    def find_callees(self, class_name: str, method_name: str) -> ToolResponse:
        """Everything `class_name.method_name` calls out to (methods, repositories,
        exceptions thrown) — the inverse of find_callers."""
        node_id = self._resolve_method_node(class_name, method_name)
        if node_id is None:
            return self._not_found("find_callees",
                                   {"class_name": class_name, "method_name": method_name},
                                   f"{class_name}.{method_name}")
        node = self.graph.node(node_id) or {}
        attrs = node.get("attributes", {})
        outbound = self.graph.neighbors(node_id, direction="out")
        callees = [{"edge": nb["edge_type"], "name": (nb.get("node") or {}).get("name"),
                    "type": (nb.get("node") or {}).get("type"),
                    "service": (nb.get("node") or {}).get("service")}
                   for nb in outbound if nb.get("node")]
        data = {
            "target": {"class": class_name, "method": method_name, "node_id": node_id},
            "callees": callees,
            "called_methods": attrs.get("called_methods", []),
            "delegates_to_repos": attrs.get("delegates_to_repos", []),
            "thrown_exceptions": attrs.get("thrown_exceptions", []),
            "total_callees": len(callees),
        }
        lines = [f"# Callees of `{class_name}.{method_name}`", "",
                 f"**{len(callees)} outbound call(s) resolved in the graph.**", ""]
        for c in callees:
            lines.append(f"- [{c['edge']}] `{c['name']}` ({c['type']}) [{c['service']}]")
        if attrs.get("called_methods"):
            lines.append("\n## Raw call expressions (source-level)")
            for call in attrs["called_methods"][:20]:
                lines.append(f"- `{call}`")
        if attrs.get("thrown_exceptions"):
            lines.append("\n## Thrown exceptions")
            lines.append(", ".join(f"`{e}`" for e in attrs["thrown_exceptions"]))
        return ToolResponse(
            tool="find_callees", query={"class_name": class_name, "method_name": method_name},
            summary=f"{len(callees)} callee(s) resolved for `{class_name}.{method_name}`.",
            data=data, markdown="\n".join(lines),
        )
    def trace_execution_path(self, source: str, target: str) -> ToolResponse:
        """Full path(s) through the call/dependency graph from `source` to
        `target` (endpoint, class, method, or service names)."""
        src_matches = self.graph.find_nodes(source, limit=3)
        dst_matches = self.graph.find_nodes(target, limit=3)
        if not src_matches or not dst_matches:
            missing = source if not src_matches else target
            return self._not_found("trace_execution_path", {"source": source, "target": target},
                                   missing)
        src_id, dst_id = src_matches[0]["id"], dst_matches[0]["id"]
        paths = self.graph.paths_between(src_id, dst_id, cutoff=8)
        resolved_paths = [[self.graph.node(nid) for nid in path] for path in paths]
        data = {
            "source": {"query": source, "resolved": src_matches[0]},
            "target": {"query": target, "resolved": dst_matches[0]},
            "path_count": len(paths),
            "paths": [[{"id": n.get("id"), "type": n.get("type"), "name": n.get("name"),
                       "service": n.get("service")} for n in p if n] for p in resolved_paths],
        }
        lines = [f"# Execution Path: `{source}` → `{target}`", "",
                 f"**{len(paths)} path(s) found (max 8 hops).**", ""]
        for i, p in enumerate(data["paths"], start=1):
            lines.append(f"## Path {i}")
            lines.append(" → ".join(f"`{n['name']}`({n['type']})" for n in p))
        if not paths:
            lines.append("_No direct path found within 8 hops — the entities may not be "
                         "directly connected, or belong to different subgraphs._")
        return ToolResponse(
            tool="trace_execution_path", query={"source": source, "target": target},
            summary=f"{len(paths)} path(s) from `{source}` to `{target}`.",
            data=data, markdown="\n".join(lines),
        )
    def find_api_path(self, source_service: str, target_service: str) -> ToolResponse:
        """Path of service-to-service calls connecting two services, e.g.
        "How does the checkout API reach InventoryService?"."""
        src = self._resolve_service(source_service)
        dst = self._resolve_service(target_service)
        src_id = f"service:{src}:{src}"
        dst_id = f"service:{dst}:{dst}"
        if self.graph.node(src_id) is None or self.graph.node(dst_id) is None:
            missing = source_service if self.graph.node(src_id) is None else target_service
            return self._not_found("find_api_path",
                                   {"source_service": source_service,
                                    "target_service": target_service}, missing)
        paths = self.graph.paths_between(src_id, dst_id, cutoff=6)
        dep_map = self.graph.service_dependency_map()
        data = {
            "source_service": src, "target_service": dst,
            "direct_dependency": dst in dep_map.get(src, []),
            "path_count": len(paths),
            "paths": [[(self.graph.node(nid) or {}).get("name", nid) for nid in p]
                      for p in paths],
        }
        lines = [f"# API Path: `{src}` → `{dst}`", "",
                 f"**Direct dependency:** {'Yes' if data['direct_dependency'] else 'No'}", ""]
        for i, p in enumerate(data["paths"], start=1):
            lines.append(f"{i}. " + " → ".join(f"`{s}`" for s in p))
        if not paths:
            lines.append("_No service-to-service call path found in the indexed graph._")
        return ToolResponse(
            tool="find_api_path",
            query={"source_service": source_service, "target_service": target_service},
            summary=f"{len(paths)} service path(s) from `{src}` to `{dst}`.",
            data=data, markdown="\n".join(lines),
        )
    def dependency_analysis(self, service_name: str) -> ToolResponse:
        """Library + inter-service dependencies for `service_name`."""
        base = self.dependency_report(service_name)
        svc = self._resolve_service(service_name)
        dep_map = self.graph.service_dependency_map()
        reverse_deps = sorted(s for s, deps in dep_map.items() if svc in deps)
        data = {**base.data, "depends_on_services": dep_map.get(svc, []),
                "depended_on_by_services": reverse_deps}
        md = base.markdown + (
            f"\n\n## Service Graph\n- **Depends on:** "
            f"{', '.join(dep_map.get(svc, [])) or 'none'}\n- **Depended on by:** "
            f"{', '.join(reverse_deps) or 'none'}\n"
        )
        return ToolResponse(
            tool="dependency_analysis", query={"service_name": service_name},
            summary=base.summary, data=data, markdown=md,
        )
    def search_code(self, query: str, service_name: str | None = None) -> ToolResponse:
        """Source code search using the full multi-stage retrieval pipeline
        (intent detection -> graph + vector retrieval -> rerank -> LLM ->
        answer verification), so results carry a confidence + hallucination
        check in addition to citations."""
        state = self.workflow.run(query)
        result: HybridResult = state["retrieval"]
        verification = state.get("verification", {})
        data = {
            "intent": state.get("intent"), "intent_confidence": state.get("intent_confidence"),
            "graph_evidence": state.get("graph_evidence", [])[:15],
            "results": [{
                "repo": rc.chunk.repo, "path": rc.chunk.rel_path,
                "service": rc.chunk.metadata.get("service"),
                "score": round(rc.score, 4), "excerpt": rc.chunk.text.strip()[:400],
            } for rc in result.chunks],
            "grounded_terms_ratio": verification.get("grounded_terms_ratio"),
            "possible_hallucination": verification.get("possible_hallucination", False),
        }
        md = f"# Search: _{query}_\n\n{state.get('answer', '')}\n"
        return ToolResponse(
            tool="search_code", query={"query": query, "service_name": service_name},
            summary=f"{len(result.chunks)} result(s), intent={state.get('intent')}, "
                    f"grounded={data['grounded_terms_ratio']}.",
            data=data, markdown=md, citations=citations_from(result.chunks),
        )
