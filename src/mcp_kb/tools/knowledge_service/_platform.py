"""KnowledgeService mixin: PlatformMixin (Phase 1/3 platform-parity tools).

Adds agent-facing primitives modeled on codebase-memory-mcp's tool surface:
raw graph queries, index-coverage transparency, lightweight file outlines,
dead-code detection, persistent Architecture Decision Records (ADRs), and
portable graph snapshot export/import.
"""
from __future__ import annotations

import json
from pathlib import Path

from ...graph.snapshot import export_snapshot, import_snapshot
from ...models import EdgeType, GraphEdge, NodeType, ToolResponse
from ...security.path_safety import UnsafeNameError, safe_join


class PlatformMixin:
    """Ad-hoc query, coverage, outline, dead-code and ADR tools."""

    # ------------------------------------------------------------------ #
    # query_graph — raw, read-only Cypher passthrough
    # ------------------------------------------------------------------ #
    def query_graph(self, query: str, max_rows: int = 200) -> ToolResponse:
        """Execute a read-only Cypher query directly against the knowledge graph.

        For ad-hoc questions the structured tools don't cover, e.g.
        'MATCH (e:KBNode {type:"Endpoint"}) RETURN e.name LIMIT 20'.
        """
        max_rows = max(1, min(int(max_rows), self.settings.tool_list_max_limit))
        try:
            rows = self.graph.run_cypher(query, max_rows=max_rows)
        except NotImplementedError as exc:
            return ToolResponse(
                tool="query_graph", query={"query": query}, summary=str(exc),
                data={"error": "unsupported_backend"}, markdown=f"**Not available:** {exc}",
            )
        except ValueError as exc:
            return ToolResponse(
                tool="query_graph", query={"query": query}, summary=f"Rejected: {exc}",
                data={"error": "write_query_rejected"}, markdown=f"**Rejected:** {exc}",
            )
        except Exception as exc:  # pragma: no cover - surfaced to caller
            return ToolResponse(
                tool="query_graph", query={"query": query}, summary=f"Query failed: {exc}",
                data={"error": "query_failed", "detail": str(exc)},
                markdown=f"**Query failed:** {exc}",
            )
        lines = [f"# query_graph ({len(rows)} row(s))", "", f"```cypher\n{query}\n```", ""]
        for row in rows[:50]:
            lines.append(f"- {json.dumps(row, default=str)}")
        if len(rows) > 50:
            lines.append(f"... and {len(rows) - 50} more (raw rows in `data`).")
        return ToolResponse(
            tool="query_graph", query={"query": query, "max_rows": max_rows},
            summary=f"{len(rows)} row(s) returned.",
            data={"rows": rows, "total": len(rows)},
            markdown="\n".join(lines),
        )

    # ------------------------------------------------------------------ #
    # check_index_coverage — transparency on what's actually indexed
    # ------------------------------------------------------------------ #
    def check_index_coverage(self, service_name: str | None = None) -> ToolResponse:
        """Report which files on disk are indexed, stale, or missing from the graph.

        Prevents false 'doesn't exist' claims by surfacing gaps explicitly
        instead of silently returning nothing.
        """
        from ...ingestion.repo_scanner import RepoScanner

        scanner = RepoScanner(self.settings)
        repos_root = Path(self.settings.repos_root)
        indexed_ok: list[str] = []
        stale: list[dict] = []
        not_indexed: list[str] = []
        repos = [repos_root / d.name for d in repos_root.iterdir()] if repos_root.exists() else []
        for repo_dir in repos:
            if not repo_dir.is_dir():
                continue
            repo = repo_dir.name
            if service_name and service_name.lower() not in repo.lower():
                continue
            known = self._meta.known_hashes(repo)
            seen_on_disk: set[str] = set()
            for source_file in scanner.scan(repo, repo_dir):
                seen_on_disk.add(source_file.rel_path)
                known_hash = known.get(source_file.rel_path)
                if known_hash is None:
                    not_indexed.append(f"{repo}/{source_file.rel_path}")
                elif known_hash != source_file.sha256:
                    stale.append({"file": f"{repo}/{source_file.rel_path}",
                                  "reason": "content_changed_since_last_index"})
                else:
                    indexed_ok.append(f"{repo}/{source_file.rel_path}")
            for rel_path in known:
                if rel_path not in seen_on_disk:
                    stale.append({"file": f"{repo}/{rel_path}", "reason": "deleted_from_disk"})
        data = {
            "coverage": {
                "indexed_ok": indexed_ok[:200],
                "stale": stale[:200],
                "not_indexed": not_indexed[:200],
            },
            "totals": {"indexed_ok": len(indexed_ok), "stale": len(stale),
                      "not_indexed": len(not_indexed)},
        }
        lines = [
            "# Index Coverage", "",
            f"- Indexed & up to date: **{len(indexed_ok)}**",
            f"- Stale (changed/deleted since last index): **{len(stale)}**",
            f"- On disk but never indexed: **{len(not_indexed)}**", "",
        ]
        if stale:
            lines.append("### Stale files")
            lines += [f"- `{s['file']}` — {s['reason']}" for s in stale[:20]]
        if not_indexed:
            lines.append("### Not indexed")
            lines += [f"- `{f}`" for f in not_indexed[:20]]
        return ToolResponse(
            tool="check_index_coverage", query={"service_name": service_name},
            summary=f"{len(indexed_ok)} up to date, {len(stale)} stale, "
                    f"{len(not_indexed)} never indexed.",
            data=data, markdown="\n".join(lines),
        )

    # ------------------------------------------------------------------ #
    # get_file_outline — cheap symbol listing without a full parse/graph dive
    # ------------------------------------------------------------------ #
    def get_file_outline(self, file_path: str, limit: int = 100,
                         offset: int = 0) -> ToolResponse:
        """List the declarations (classes, methods, fields, endpoints) in one file
        with line ranges — a cheap alternative to explain_code for orientation."""
        limit = max(1, min(int(limit), self.settings.tool_list_max_limit))
        nodes = self.graph.find_by_file(file_path)
        nodes = sorted(nodes, key=lambda n: n.get("start_line") or 0)
        total = len(nodes)
        page = nodes[offset:offset + limit]
        outline = [
            {"name": n.get("name"), "type": n.get("type"),
             "start_line": n.get("start_line"), "end_line": n.get("end_line")}
            for n in page
        ]
        lines = [f"# Outline: `{file_path}` ({total} declaration(s))", ""]
        for item in outline:
            span = f"L{item['start_line']}-{item['end_line']}" if item["start_line"] else ""
            lines.append(f"- **{item['type']}** `{item['name']}` {span}")
        if total > offset + limit:
            lines.append(f"\n... {total - offset - limit} more (increase `limit`/`offset`).")
        return ToolResponse(
            tool="get_file_outline", query={"file_path": file_path, "limit": limit, "offset": offset},
            summary=f"{total} declaration(s) in {file_path}.",
            data={"outline": outline, "total": total, "offset": offset,
                  "limit": limit, "has_more": offset + limit < total},
            markdown="\n".join(lines),
        )

    # ------------------------------------------------------------------ #
    # find_dead_code — nodes with zero inbound reference edges
    # ------------------------------------------------------------------ #
    def find_dead_code(self, service_name: str | None = None, limit: int = 100) -> ToolResponse:
        """Find methods/functions with no inbound CALLS edges (excluding
        entry points: endpoints, Kafka listeners, scheduled/main methods)."""
        limit = max(1, min(int(limit), self.settings.tool_list_max_limit))
        svc = self._resolve_service(service_name) if service_name else None
        candidates = self.graph.nodes_by_type(NodeType.METHOD, service=svc)
        entry_point_hint = ("controller", "listener", "scheduled", "main", "@eventlistener")
        dead: list[dict] = []
        for node in candidates:
            name = (node.get("name") or "").lower()
            cls = (node.get("class") or node.get("qualified_name") or "").lower()
            if any(hint in name or hint in cls for hint in entry_point_hint):
                continue
            callers = self.graph.neighbors(node["id"], edge_types={EdgeType.CALLS}, direction="in")
            if not callers:
                dead.append({"name": node.get("name"), "class": node.get("class"),
                            "file": node.get("file"), "service": node.get("service")})
        dead = dead[:limit]
        lines = [f"# Potentially Dead Code ({len(dead)} method(s))", "",
                 "_Heuristic: zero inbound CALLS edges, excluding likely entry points "
                 "(controllers, listeners, scheduled/main methods). Verify before deleting._", ""]
        for d in dead:
            lines.append(f"- `{d['class']}.{d['name']}` — `{d['file']}` [{d['service']}]")
        return ToolResponse(
            tool="find_dead_code", query={"service_name": service_name},
            summary=f"{len(dead)} potentially unused method(s) found.",
            data={"candidates": dead, "total": len(dead)},
            markdown="\n".join(lines),
        )

    # ------------------------------------------------------------------ #
    # manage_adr — persistent Architecture Decision Records
    # ------------------------------------------------------------------ #
    def manage_adr(self, project: str, mode: str = "get", content: str | None = None,
                   section_updates: dict[str, str] | None = None) -> ToolResponse:
        """Get, replace, or partially update an Architecture Decision Record
        document for a project. mode: 'get' | 'update' | 'set_sections' | 'sections'."""
        adr_dir = Path(self.settings.adr_store_dir)
        adr_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = safe_join(adr_dir, project, suffix=".md")
        except UnsafeNameError as exc:
            return ToolResponse(
                tool="manage_adr", query={"project": project, "mode": mode},
                summary=f"Rejected: {exc}", data={"error": "unsafe_name"},
                markdown=f"**Rejected:** {exc}",
            )

        if mode == "get":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            return ToolResponse(
                tool="manage_adr", query={"project": project, "mode": mode},
                summary=f"ADR for '{project}' ({'exists' if path.exists() else 'empty'}).",
                data={"content": text, "exists": path.exists()},
                markdown=text or f"_No ADR recorded yet for `{project}`._",
            )

        if mode == "sections":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            headings = [ln.lstrip("#").strip() for ln in text.splitlines() if ln.startswith("#")]
            return ToolResponse(
                tool="manage_adr", query={"project": project, "mode": mode},
                summary=f"{len(headings)} section(s).",
                data={"sections": headings},
                markdown="\n".join(f"- {h}" for h in headings) or "_No sections._",
            )

        if mode == "update":
            path.write_text(content or "", encoding="utf-8")
            return ToolResponse(
                tool="manage_adr", query={"project": project, "mode": mode},
                summary=f"ADR for '{project}' replaced.",
                data={"bytes_written": len(content or "")},
                markdown=f"ADR for `{project}` updated ({len(content or '')} bytes).",
            )

        if mode == "set_sections":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            for heading, new_body in (section_updates or {}).items():
                marker = f"## {heading}"
                if marker in text:
                    before, _, rest = text.partition(marker)
                    _, _, after_rest = rest.partition("\n## ")
                    remainder = ("\n## " + after_rest) if after_rest else ""
                    text = f"{before}{marker}\n{new_body}\n{remainder}"
                else:
                    text = f"{text}\n\n{marker}\n{new_body}\n"
            path.write_text(text, encoding="utf-8")
            return ToolResponse(
                tool="manage_adr", query={"project": project, "mode": mode},
                summary=f"{len(section_updates or {})} section(s) updated.",
                data={"content": text},
                markdown=f"ADR sections updated for `{project}`.",
            )

        return ToolResponse(
            tool="manage_adr", query={"project": project, "mode": mode},
            summary=f"Unknown mode '{mode}'.",
            data={"error": "unknown_mode"},
            markdown=f"**Unknown mode** `{mode}`. Use get|update|set_sections|sections.",
        )

    # ------------------------------------------------------------------ #
    # compare_graphs — arbitrary node/edge diff between two snapshots
    # ------------------------------------------------------------------ #
    def compare_graphs(self, base_service: str, target_service: str,
                       limit: int = 100) -> ToolResponse:
        """Diff nodes between two services/snapshots (added/removed by qualified name).
        Complements compare_versions (git-tag based) for arbitrary graph comparisons."""
        limit = max(1, min(int(limit), self.settings.tool_list_max_limit))
        base = self._resolve_service(base_service)
        target = self._resolve_service(target_service)
        base_names = {n.get("qualified_name") or n.get("name") for n in self.graph.all_nodes(base)}
        target_names = {n.get("qualified_name") or n.get("name") for n in self.graph.all_nodes(target)}
        added = sorted(target_names - base_names)[:limit]
        removed = sorted(base_names - target_names)[:limit]
        lines = [f"# compare_graphs: `{base}` vs `{target}`", "",
                 f"- Added in `{target}`: **{len(added)}**",
                 f"- Removed vs `{base}`: **{len(removed)}**", ""]
        if added:
            lines.append("### Added\n" + "\n".join(f"- `{n}`" for n in added[:20]))
        if removed:
            lines.append("### Removed\n" + "\n".join(f"- `{n}`" for n in removed[:20]))
        return ToolResponse(
            tool="compare_graphs", query={"base_service": base, "target_service": target},
            summary=f"{len(added)} added, {len(removed)} removed.",
            data={"added": added, "removed": removed},
            markdown="\n".join(lines),
        )

    # ------------------------------------------------------------------ #
    # Phase 3: portable graph snapshot export/import
    # ------------------------------------------------------------------ #
    def export_snapshot(self, name: str, service: str | None = None) -> ToolResponse:
        """Export the graph (or one service) to a portable, gzip-compressed
        snapshot file under MCP_KB_SNAPSHOT_DIR — share it instead of making
        a teammate re-run the full ingestion pipeline."""
        try:
            path = safe_join(Path(self.settings.snapshot_dir), name, suffix=".json.gz")
        except UnsafeNameError as exc:
            return ToolResponse(
                tool="export_snapshot", query={"name": name, "service": service},
                summary=f"Rejected: {exc}", data={"error": "unsafe_name"},
                markdown=f"**Rejected:** {exc}",
            )
        try:
            counts = export_snapshot(self.graph, path, service=service)
        except NotImplementedError as exc:
            return ToolResponse(
                tool="export_snapshot", query={"name": name, "service": service},
                summary=str(exc), data={"error": "unsupported_backend"},
                markdown=f"**Not available:** {exc}",
            )
        return ToolResponse(
            tool="export_snapshot", query={"name": name, "service": service},
            summary=f"Exported {counts['nodes']} nodes, {counts['edges']} edges to {path}.",
            data={"path": str(path), **counts},
            markdown=f"Snapshot written to `{path}` ({counts['nodes']} nodes, "
                     f"{counts['edges']} edges).",
        )

    def import_snapshot(self, name: str) -> ToolResponse:
        """Import a previously exported snapshot (see export_snapshot) into the
        live graph. Upserts by node/edge id — safe to re-run."""
        try:
            path = safe_join(Path(self.settings.snapshot_dir), name, suffix=".json.gz")
        except UnsafeNameError as exc:
            return ToolResponse(
                tool="import_snapshot", query={"name": name},
                summary=f"Rejected: {exc}", data={"error": "unsafe_name"},
                markdown=f"**Rejected:** {exc}",
            )
        if not path.exists():
            return ToolResponse(
                tool="import_snapshot", query={"name": name},
                summary=f"No snapshot found at {path}.",
                data={"error": "not_found"}, markdown=f"**Not found:** `{path}`",
            )
        counts = import_snapshot(self.graph, path)
        return ToolResponse(
            tool="import_snapshot", query={"name": name},
            summary=(f"Imported {counts['nodes']} nodes, {counts['edges']} edges "
                    f"(skipped {counts['skipped_nodes']} nodes, {counts['skipped_edges']} edges)."),
            data=counts,
            markdown=f"Imported from `{path}`: {counts['nodes']} nodes, {counts['edges']} edges.",
        )

    # ------------------------------------------------------------------ #
    # Follow-on: runtime trace ingestion (opt-in overlay, never mutates
    # static CALLS edges or creates placeholder nodes — mirrors CBM's
    # RUNTIME_TRACE_MODEL.md design contract).
    # ------------------------------------------------------------------ #
    def ingest_traces(self, service_name: str, traces: list[dict]) -> ToolResponse:
        """Ingest observed runtime call traces as an opt-in overlay.

        Each trace is `{"caller": "<qualified_name>", "callee": "<qualified_name>",
        "count": <int>}`. Only resolves against methods that already exist in
        the static graph (matched by exact or trailing-name match) — traces
        for unknown methods are reported as skipped, never used to fabricate
        new nodes. Produces `RUNTIME_CALL` edges, distinct from static `CALLS`
        edges, so `trace_execution_path`/`call_graph` callers can opt in to
        this overlay explicitly rather than have it silently blend in.
        """
        service = self._resolve_service(service_name) if service_name else None
        method_nodes = self.graph.nodes_by_type(NodeType.METHOD, service=service)
        by_qualified = {n.get("attributes", {}).get("qualified_name"): n["id"]
                       for n in method_nodes if n.get("attributes", {}).get("qualified_name")}
        by_suffix: dict[str, list[str]] = {}
        for qname, node_id in by_qualified.items():
            by_suffix.setdefault(qname.rsplit(".", 1)[-1], []).append(node_id)

        def _resolve(name: str) -> str | None:
            if name in by_qualified:
                return by_qualified[name]
            candidates = by_suffix.get(name.rsplit(".", 1)[-1], [])
            return candidates[0] if len(candidates) == 1 else None

        edges = []
        accepted = 0
        skipped: list[dict] = []
        for trace in traces:
            caller_name = str(trace.get("caller", ""))
            callee_name = str(trace.get("callee", ""))
            count = int(trace.get("count", 1))
            caller_id = _resolve(caller_name)
            callee_id = _resolve(callee_name)
            if not caller_id or not callee_id:
                skipped.append({"caller": caller_name, "callee": callee_name,
                               "reason": "caller_not_found" if not caller_id else "callee_not_found"})
                continue
            edges.append(GraphEdge(src=caller_id, dst=callee_id, type=EdgeType.RUNTIME_CALL,
                                   attributes={"count": count, "source": "runtime_trace"}))
            accepted += 1

        if edges:
            self.graph.add_many([], edges)
            self.graph.save()

        return ToolResponse(
            tool="ingest_traces", query={"service_name": service_name, "trace_count": len(traces)},
            summary=f"{accepted} trace(s) ingested, {len(skipped)} skipped (unresolved method).",
            data={"accepted": accepted, "skipped": skipped[:50]},
            markdown=(f"# Runtime Trace Ingestion\n\n- Accepted: **{accepted}**\n"
                     f"- Skipped (unresolved): **{len(skipped)}**\n\n"
                     "These are `RUNTIME_CALL` edges — an opt-in overlay distinct from "
                     "statically-derived `CALLS` edges."),
        )
