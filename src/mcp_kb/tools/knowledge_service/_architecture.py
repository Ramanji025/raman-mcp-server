"""KnowledgeService mixin: ArchitectureMixin (architecture domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from pathlib import Path

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...retrieval.hybrid_retriever import HybridResult
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class ArchitectureMixin:
    """Architecture summary, branch/version diffing, and Kafka topology tools."""
    def generate_architecture_summary(self) -> ToolResponse:
        """Ecosystem-wide overview: services, dependencies, event topics, gateways, databases."""
        stats = self.graph.stats()
        dep_map = self.graph.service_dependency_map()
        services = self.graph.services()
        topics = self.graph.nodes_by_type(NodeType.TOPIC)
        gateways = self.graph.nodes_by_type(NodeType.GATEWAY)
        queues = self.graph.nodes_by_type(NodeType.QUEUE)
        databases = self.graph.nodes_by_type(NodeType.DATABASE)
        data = {
            "stats": stats,
            "services": services,
            "service_dependencies": dep_map,
            "event_topics": sorted({t["name"] for t in topics}),
            "gateways": [g["name"] for g in gateways],
            "queues": sorted({q["name"] for q in queues}),
            "databases": sorted({d["name"] for d in databases}),
            "mermaid": self._mermaid_dependency_graph(dep_map),
        }
        md = self._render_architecture_md(data)
        return ToolResponse(
            tool="generate_architecture_summary", query={},
            summary=(f"{len(services)} services, {stats['nodes']} nodes, "
                     f"{stats['edges']} edges, {len(data['event_topics'])} topics, "
                     f"{len(data['queues'])} queues, {len(data['databases'])} databases."),
            data=data, markdown=md,
        )
    def _render_architecture_md(self, d: dict) -> str:
        s = d["stats"]
        lines = ["# Architecture summary", "",
                 f"- **Services:** {len(d['services'])}",
                 f"- **Graph nodes:** {s['nodes']} | **edges:** {s['edges']}",
                 f"- **Event topics:** {len(d['event_topics'])}", "",
                 "## Node breakdown"]
        for t, n in sorted(s.get("by_type", {}).items()):
            lines.append(f"- {t}: {n}")
        lines.append("\n## Service dependency graph\n")
        lines.append("```mermaid\n" + d["mermaid"] + "\n```")
        return "\n".join(lines)
    @staticmethod
    def _mermaid_dependency_graph(dep_map: dict[str, list[str]]) -> str:
        lines = ["graph LR"]
        if not dep_map:
            lines.append("  empty[No dependencies indexed]")
        for src, targets in sorted(dep_map.items()):
            for dst in targets:
                lines.append(f"  {src.replace('-', '_')} --> {dst.replace('-', '_')}")
        return "\n".join(lines)
    def compare_branches(self, repo_name: str, branch1: str, branch2: str) -> ToolResponse:
        """Diff two git branches and analyse the change impact on APIs, services, entities."""
        from git import InvalidGitRepositoryError, Repo
        repo_path = Path(self.settings.repos_root) / repo_name
        if not repo_path.exists():
            return self._not_found("compare_branches",
                                   {"repo_name": repo_name}, repo_name)
        try:
            git_repo = Repo(str(repo_path))
        except InvalidGitRepositoryError:
            return self._not_found("compare_branches", {"repo_name": repo_name}, repo_name)

        try:
            diff_index = git_repo.commit(branch1).diff(git_repo.commit(branch2))
        except Exception as e:
            return ToolResponse(
                tool="compare_branches",
                query={"repo_name": repo_name, "branch1": branch1, "branch2": branch2},
                summary=f"Could not diff branches: {e}",
                data={"error": str(e)}, markdown=f"**Error**: {e}",
            )

        changed_files: list[dict] = []
        java_changed: list[str] = []
        for d in diff_index:
            change_type = d.change_type  # A/M/D/R
            path = d.b_path or d.a_path
            lines_added = lines_removed = 0
            try:
                patch = d.diff.decode("utf-8", errors="replace") if d.diff else ""
                lines_added = patch.count("\n+")
                lines_removed = patch.count("\n-")
            except Exception:
                patch = ""
            changed_files.append({
                "path": path, "change": change_type,
                "lines_added": lines_added, "lines_removed": lines_removed,
            })
            if path.endswith(".java"):
                java_changed.append(path)

        # Cross-reference with knowledge graph.
        impacted_endpoints: list[dict] = []
        impacted_services_set: set[str] = set()
        for rel_path in java_changed:
            nodes = self.graph.find_by_file(rel_path)
            for n in nodes:
                ntype = n.get("type", "")
                if ntype == "Endpoint":
                    impacted_endpoints.append({"path": rel_path,
                                               "endpoint": n.get("name"),
                                               "service": n.get("service")})
                if n.get("service"):
                    impacted_services_set.add(n["service"])

        # Retrieve semantic context for the changed areas.
        changed_names = " ".join(p.split("/")[-1].replace(".java", "") for p in java_changed[:10])
        retrieval = self.retriever.retrieve(
            f"changes in {changed_names}", collections=("code",), top_k=6,
        ) if changed_names else None

        data = {
            "repo": repo_name, "branch1": branch1, "branch2": branch2,
            "total_files_changed": len(changed_files),
            "java_files_changed": len(java_changed),
            "changed_files": changed_files,
            "impacted_endpoints": impacted_endpoints,
            "impacted_services": sorted(impacted_services_set),
            "change_summary": (
                f"{len(changed_files)} files changed ({len(java_changed)} Java), "
                f"affecting {len(impacted_endpoints)} endpoint(s) across "
                f"{len(impacted_services_set)} service(s)."
            ),
        }
        md = self._render_branch_diff_md(branch1, branch2, data, retrieval)
        return ToolResponse(
            tool="compare_branches",
            query={"repo_name": repo_name, "branch1": branch1, "branch2": branch2},
            summary=data["change_summary"], data=data, markdown=md,
            citations=citations_from(retrieval.chunks) if retrieval else [],
        )
    @staticmethod
    def _render_branch_diff_md(b1: str, b2: str, d: dict,
                                retrieval: HybridResult | None) -> str:
        lines = [f"# Branch Comparison: `{b1}` → `{b2}`",
                 f"**Repo:** {d['repo']}",
                 f"**{d['change_summary']}**", "",
                 "## Changed Files"]
        for f in d["changed_files"][:40]:
            lines.append(f"- [{f['change']}] `{f['path']}` "
                         f"(+{f['lines_added']} / -{f['lines_removed']})")
        if d["impacted_endpoints"]:
            lines += ["", "## Impacted Endpoints"]
            for ep in d["impacted_endpoints"]:
                lines.append(f"- `{ep['endpoint']}` in `{ep['service']}`")
        if d["impacted_services"]:
            lines += ["", "## Impacted Services",
                      ", ".join(f"`{s}`" for s in d["impacted_services"])]
        if retrieval and retrieval.chunks:
            lines += ["", "## Related Context"]
            for rc in retrieval.chunks[:4]:
                lines.append(f"> `{rc.chunk.rel_path}` — {rc.chunk.text[:200]}")
        return "\n".join(lines)
    def explain_architecture(self) -> ToolResponse:
        """Alias for generate_architecture_summary under the `explain_architecture` tool name."""
        base = self.generate_architecture_summary()
        return ToolResponse(tool="explain_architecture", query={}, summary=base.summary,
                            data=base.data, markdown=base.markdown, citations=base.citations)
    def kafka_topology(self) -> ToolResponse:
        """Full Kafka event topology: producer → topic → consumer, unmatched events."""
        producers = self.graph.nodes_by_type(NodeType.KAFKA_PRODUCER)
        consumers = self.graph.nodes_by_type(NodeType.KAFKA_CONSUMER)
        topics = self.graph.nodes_by_type(NodeType.TOPIC)

        producer_topics = {p.get("attributes", {}).get("topic") for p in producers
                           if p.get("attributes", {}).get("topic")}
        consumer_topics = {c.get("attributes", {}).get("topic") for c in consumers
                           if c.get("attributes", {}).get("topic")}
        all_topic_names = {t.get("name") for t in topics}

        unmatched_producers = producer_topics - consumer_topics
        unmatched_consumers = consumer_topics - producer_topics

        flows: list[dict] = []
        for topic_name in producer_topics | consumer_topics:
            prods = [p for p in producers if p.get("attributes", {}).get("topic") == topic_name]
            cons  = [c for c in consumers if c.get("attributes", {}).get("topic") == topic_name]
            flows.append({
                "topic": topic_name,
                "producers": [{"service": p.get("service"), "class": p.get("name"),
                                "method": p.get("attributes", {}).get("method")}
                               for p in prods],
                "consumers": [{"service": c.get("service"), "class": c.get("name"),
                                "method": c.get("attributes", {}).get("method")}
                               for c in cons],
                "status": ("matched" if prods and cons else
                           "orphan_producer" if prods else "orphan_consumer"),
            })

        data = {
            "total_topics": len(all_topic_names),
            "total_producers": len(producers),
            "total_consumers": len(consumers),
            "flows": flows,
            "unmatched_producers": sorted(unmatched_producers),
            "unmatched_consumers": sorted(unmatched_consumers),
            "mermaid": self._kafka_mermaid(flows),
        }
        md = self._render_kafka_topology_md(data)
        return ToolResponse(
            tool="kafka_topology", query={},
            summary=(f"Kafka topology: {len(all_topic_names)} topics, "
                     f"{len(producers)} producers, {len(consumers)} consumers, "
                     f"{len(unmatched_producers)} unmatched producer topics."),
            data=data, markdown=md,
        )
    @staticmethod
    def _render_kafka_topology_md(d: dict) -> str:
        lines = ["# Kafka Event Topology",
                 f"**{d['total_topics']} topics** | "
                 f"**{d['total_producers']} producers** | "
                 f"**{d['total_consumers']} consumers**", ""]
        for flow in d["flows"]:
            status_icon = "✅" if flow["status"] == "matched" else "⚠️"
            lines.append(f"## {status_icon} Topic: `{flow['topic']}`")
            for p in flow["producers"]:
                lines.append(f"  **Producer**: `{p['service']}.{p['class']}.{p['method']}`")
            for c in flow["consumers"]:
                lines.append(f"  **Consumer**: `{c['service']}.{c['class']}.{c['method']}`")
            lines.append("")
        if d["unmatched_producers"]:
            lines += ["## ⚠️ Unmatched Producer Topics (no consumer)"]
            for t in d["unmatched_producers"]:
                lines.append(f"- `{t}`")
        if d["unmatched_consumers"]:
            lines += ["", "## ⚠️ Unmatched Consumer Topics (no producer indexed)"]
            for t in d["unmatched_consumers"]:
                lines.append(f"- `{t}`")
        lines += ["", "## Event Flow Diagram", "```mermaid", d["mermaid"], "```"]
        return "\n".join(lines)
    @staticmethod
    def _kafka_mermaid(flows: list[dict]) -> str:
        lines = ["graph LR"]
        for flow in flows:
            topic = flow["topic"].replace("-", "_").replace(".", "_")
            for p in flow["producers"]:
                src = f"{(p['service'] or 'unknown').replace('-','_')}_{(p['class'] or 'P').replace('-','_')}"
                lines.append(f"  {src} -->|produce| {topic}[{flow['topic']}]")
            for c in flow["consumers"]:
                dst = f"{(c['service'] or 'unknown').replace('-','_')}_{(c['class'] or 'C').replace('-','_')}"
                lines.append(f"  {topic}[{flow['topic']}] -->|consume| {dst}")
        return "\n".join(lines)
    def compare_versions(self, repo_name: str, version_a: str, version_b: str) -> ToolResponse:
        """Diff two versions (release tags, branches, or commit SHAs) of a
        repo and analyse the change impact — e.g. "compare release 2.7 and 2.8"."""
        from ..ingestion.git_manager import GitManager

        git = GitManager(self.settings)
        try:
            ref_a = git.resolve_ref(repo_name, version_a)
            ref_b = git.resolve_ref(repo_name, version_b)
        except Exception as exc:
            return ToolResponse(
                tool="compare_versions",
                query={"repo_name": repo_name, "version_a": version_a, "version_b": version_b},
                summary=f"Could not resolve versions: {exc}", data={"error": str(exc)},
                markdown=f"**Error**: {exc}",
            )
        base = self.compare_branches(repo_name, version_a, version_b)
        data = {**base.data, "version_a": ref_a, "version_b": ref_b}
        md = (f"# Compare `{version_a}` ({ref_a['commit'][:10]}) vs "
              f"`{version_b}` ({ref_b['commit'][:10]})\n\n" + base.markdown)
        return ToolResponse(
            tool="compare_versions",
            query={"repo_name": repo_name, "version_a": version_a, "version_b": version_b},
            summary=base.summary, data=data, markdown=md, citations=base.citations,
        )
