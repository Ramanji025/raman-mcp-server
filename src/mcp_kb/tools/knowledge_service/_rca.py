"""KnowledgeService mixin: RcaMixin (rca domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...rca.exception_analyzer import analyze_exception
from ...rca.severity_scoring import classify_severity
from ...retrieval.rag import citations_from

log = get_logger(__name__)


from ._helpers import _infer_method_purpose


class RcaMixin:
    """Root-cause analysis: defect changes, exceptions, incidents, story analysis."""
    def analyze_user_story(self, story_id_or_description: str) -> ToolResponse:
        """Fetch a Rally user story and map it to affected microservices, classes and methods."""
        from ..integrations.rally_client import make_rally_client_from_settings

        story: dict = {}
        rally_error: str | None = None
        query_text = story_id_or_description

        # Try Rally API if it looks like a story ID (US/DE/TA + digits).
        if re.match(r"^(US|DE|TA|F|I)\d+$", story_id_or_description.strip(), re.IGNORECASE):
            client = make_rally_client_from_settings(self.settings)
            if client:
                try:
                    story = client.get_story(story_id_or_description.strip())
                    if story:
                        query_text = (
                            f"{story.get('name', '')} "
                            f"{story.get('description', '')} "
                            f"{story.get('acceptance_criteria', '')}"
                        )
                except Exception as e:
                    rally_error = str(e)
            else:
                rally_error = "Rally not configured — set RALLY_API_KEY in .env"
        else:
            # Treat the input as a plain description.
            story = {
                "id": "manual",
                "name": story_id_or_description[:120],
                "description": story_id_or_description,
                "acceptance_criteria": "",
                "state": "provided",
            }

        # Semantic search across all services.
        retrieval = self.retriever.retrieve(
            query_text, collections=("code", "docs", "architecture"), top_k=15,
        )

        # Group hits by service and class.
        service_hits: dict[str, dict] = {}
        for rc in retrieval.chunks:
            svc = rc.chunk.metadata.get("service") or rc.chunk.repo
            cls = rc.chunk.metadata.get("class", "")
            method = rc.chunk.metadata.get("method", "")
            service_hits.setdefault(svc, {"classes": {}, "score_sum": 0.0})
            service_hits[svc]["score_sum"] += rc.score
            service_hits[svc]["classes"].setdefault(cls, {"methods": [], "file": rc.chunk.rel_path})
            if method:
                service_hits[svc]["classes"][cls]["methods"].append({
                    "method": method, "score": round(rc.score, 3),
                    "lines": f"{rc.chunk.start_line}-{rc.chunk.end_line}",
                    "file": rc.chunk.rel_path,
                })

        affected_services = sorted(
            [{"service": svc, "relevance_score": round(v["score_sum"], 3),
              "affected_classes": [
                  {"class": cls, "file": cinfo["file"],
                   "affected_methods": cinfo["methods"][:5]}
                  for cls, cinfo in v["classes"].items()
              ]}
             for svc, v in service_hits.items()],
            key=lambda x: -x["relevance_score"],
        )

        # Suggest implementation plan.
        impl_plan = self._story_implementation_plan(story, affected_services)
        contract = self._story_contract(query_text, retrieval)
        data = {
            "story": story,
            "rally_error": rally_error,
            "affected_services": affected_services,
            "implementation_plan": impl_plan,
            "story_contract": contract.model_dump(),
            "total_code_hits": len(retrieval.chunks),
        }
        md = self._render_story_analysis_md(story, affected_services, impl_plan, rally_error)
        md += "\n" + contract.as_markdown()
        return ToolResponse(
            tool="analyze_user_story",
            query={"story_id_or_description": story_id_or_description},
            summary=(f"Story '{story.get('name', story_id_or_description)[:60]}': "
                     f"{len(affected_services)} service(s), "
                     f"{len(contract.files_to_touch)} file(s) to touch."),
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    def _story_contract(self, story_text: str, retrieval) -> Any:
        from ...nlp.story_contract import build_story_contract

        endpoints = []
        for n in self.graph.nodes_by_type(NodeType.ENDPOINT):
            attrs = n.get("attributes") or {}
            endpoints.append({
                "name": n.get("name"), "service": n.get("service"),
                "path": attrs.get("path"), "http_method": attrs.get("http_method"),
                "file": attrs.get("file"),
            })
        methods = []
        for n in self.graph.nodes_by_type(NodeType.METHOD):
            attrs = n.get("attributes") or {}
            methods.append({
                "name": n.get("name"), "service": n.get("service"),
                "class": attrs.get("class"), "file": attrs.get("file"),
            })
        tests = []
        for n in self.graph.nodes_by_type(NodeType.TEST_CLASS):
            attrs = n.get("attributes") or {}
            tests.append({
                "name": n.get("name"), "service": n.get("service"),
                "tested_class": attrs.get("tested_class"), "file": attrs.get("file"),
            })
        retrieval_files = []
        if retrieval is not None:
            retrieval_files = [rc.chunk.rel_path for rc in retrieval.chunks if rc.chunk.rel_path]
        return build_story_contract(
            story_text, endpoints=endpoints, methods=methods, tests=tests,
            retrieval_files=retrieval_files,
        )
    @staticmethod
    def _story_implementation_plan(story: dict, services: list[dict]) -> list[dict]:
        plan = []
        for i, svc_hit in enumerate(services[:5], start=1):
            svc = svc_hit["service"]
            classes = svc_hit["affected_classes"]
            plan.append({
                "step": i,
                "service": svc,
                "action": f"Review and modify {len(classes)} class(es) in `{svc}`",
                "classes_to_change": [c["class"] for c in classes[:5]],
                "recommended_checks": [
                    f"Run `find_antipatterns('{svc}')` before making changes",
                    "Run `impact_analysis` on changed entities",
                    "Update/add tests for changed classes",
                    "Run `security_audit` after changes to verify no regressions",
                ],
            })
        return plan
    @staticmethod
    def _render_story_analysis_md(story: dict, services: list[dict],
                                   plan: list[dict], rally_error: str | None) -> str:
        lines = [f"# User Story Analysis: `{story.get('id', 'manual')}`",
                 f"**{story.get('name', '')}**", ""]
        if rally_error:
            lines.append(f"> ⚠️ Rally: {rally_error}\n")
        if story.get("description"):
            lines += ["## Description", story["description"][:600], ""]
        if story.get("acceptance_criteria"):
            lines += ["## Acceptance Criteria", story["acceptance_criteria"][:600], ""]
        if story.get("state"):
            lines.append(f"**State**: {story['state']} | **Priority**: {story.get('priority', '?')} | "
                         f"**Estimate**: {story.get('estimate', '?')} pts\n")
        lines += ["## Affected Services", ""]
        for svc_hit in services:
            lines.append(f"### `{svc_hit['service']}` (relevance: {svc_hit['relevance_score']})")
            for cls_info in svc_hit["affected_classes"][:5]:
                lines.append(f"  - **`{cls_info['class']}`** (`{cls_info['file']}`)")
                for mth in cls_info["affected_methods"][:3]:
                    lines.append(f"    - method `{mth['method']}` (score: {mth['score']}, lines {mth['lines']})")
            lines.append("")
        lines += ["## Implementation Plan", ""]
        for step in plan:
            lines.append(f"### Step {step['step']}: `{step['service']}`")
            lines.append(f"**Action**: {step['action']}")
            if step["classes_to_change"]:
                lines.append(f"**Classes**: {', '.join(f'`{c}`' for c in step['classes_to_change'])}")
            lines.append("**Checks**:")
            for chk in step["recommended_checks"]:
                lines.append(f"  - {chk}")
            lines.append("")
        return "\n".join(lines)
    def cross_service_impact(self, change_description: str) -> ToolResponse:
        """Given a change description, identify impact across ALL microservices."""
        # Retrieve across all services — no service filter.
        retrieval = self.retriever.retrieve(
            change_description,
            collections=("code", "architecture", "docs"),
            top_k=20,
        )

        # Graph traversal — find all services and their dependency chains.
        dep_map = self.graph.service_dependency_map()
        all_services = self.graph.services()

        # Group code hits by service.
        service_hits: dict[str, list] = {}
        for rc in retrieval.chunks:
            svc = rc.chunk.metadata.get("service") or rc.chunk.repo
            service_hits.setdefault(svc, []).append({
                "class": rc.chunk.metadata.get("class", ""),
                "method": rc.chunk.metadata.get("method", ""),
                "file": rc.chunk.rel_path,
                "score": round(rc.score, 3),
                "snippet": rc.chunk.text[:300],
            })

        # For each directly-hit service, trace downstream dependencies.
        directly_affected = set(service_hits.keys())
        transitively_affected: dict[str, set[str]] = {}
        for svc in directly_affected:
            consumers = {s for s, deps in dep_map.items() if svc in deps}
            if consumers:
                transitively_affected[svc] = consumers

        # Aggregate Kafka topic impact.
        producers = self.graph.nodes_by_type(NodeType.KAFKA_PRODUCER)
        kafka_impact = []
        for p in producers:
            if p.get("service") in directly_affected:
                topic = p.get("attributes", {}).get("topic")
                if topic:
                    kafka_impact.append({
                        "topic": topic,
                        "producer_service": p.get("service"),
                        "note": "Consumer services may also be impacted",
                    })

        # Build impact matrix.
        impact_matrix = []
        for svc in all_services:
            if svc in directly_affected:
                level = "DIRECT"
                hits = service_hits.get(svc, [])
            elif any(svc in consumers for consumers in transitively_affected.values()):
                level = "TRANSITIVE"
                hits = []
            else:
                continue
            impact_matrix.append({
                "service": svc, "impact_level": level,
                "affected_classes": list({h["class"] for h in hits if h["class"]})[:10],
                "affected_methods": list({h["method"] for h in hits if h["method"]})[:10],
                "top_hits": hits[:5],
                "depends_on": dep_map.get(svc, []),
                "consumed_by": [s for s, deps in dep_map.items() if svc in deps],
            })

        # Sort: DIRECT first, then TRANSITIVE.
        impact_matrix.sort(key=lambda x: 0 if x["impact_level"] == "DIRECT" else 1)

        data = {
            "change_description": change_description,
            "total_services_in_kb": len(all_services),
            "directly_affected_count": len(directly_affected),
            "transitively_affected_count": sum(len(v) for v in transitively_affected.values()),
            "kafka_topic_impact": kafka_impact,
            "impact_matrix": impact_matrix,
            "recommendation": self._cross_service_recommendations(impact_matrix, kafka_impact),
        }
        md = self._render_cross_service_md(change_description, data)
        return ToolResponse(
            tool="cross_service_impact",
            query={"change_description": change_description},
            summary=(f"Cross-service impact of '{change_description[:60]}': "
                     f"{len(directly_affected)} direct, "
                     f"{sum(len(v) for v in transitively_affected.values())} transitive."),
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    @staticmethod
    def _cross_service_recommendations(matrix: list[dict], kafka: list[dict]) -> list[str]:
        recs = []
        direct = [m for m in matrix if m["impact_level"] == "DIRECT"]
        transitive = [m for m in matrix if m["impact_level"] == "TRANSITIVE"]
        if direct:
            recs.append(f"Directly modify {len(direct)} service(s): "
                        f"{', '.join(m['service'] for m in direct)}")
        if transitive:
            recs.append(f"Notify owners of {len(transitive)} downstream service(s): "
                        f"{', '.join(m['service'] for m in transitive)}")
        if kafka:
            recs.append(f"Validate Kafka contract compatibility for "
                        f"{len(kafka)} topic(s): "
                        f"{', '.join(k['topic'] for k in kafka)}")
        recs.append("Run integration tests across all affected services.")
        recs.append("Deploy in dependency order: upstream services before downstream consumers.")
        return recs
    @staticmethod
    def _render_cross_service_md(change: str, d: dict) -> str:
        lines = ["# Cross-Service Impact Analysis",
                 f"**Change**: _{change}_", "",
                 f"**{d['directly_affected_count']} directly affected** | "
                 f"**{d['transitively_affected_count']} transitively affected** | "
                 f"**{d['total_services_in_kb']} total services in KB**", ""]
        for m in d["impact_matrix"]:
            icon = "🔴" if m["impact_level"] == "DIRECT" else "🟡"
            lines.append(f"## {icon} `{m['service']}` — {m['impact_level']}")
            if m["affected_classes"]:
                lines.append(f"**Classes**: {', '.join(f'`{c}`' for c in m['affected_classes'])}")
            if m["affected_methods"]:
                lines.append(f"**Methods**: {', '.join(f'`{m_}`' for m_ in m['affected_methods'][:8])}")
            if m["depends_on"]:
                lines.append(f"**Depends on**: {', '.join(f'`{s}`' for s in m['depends_on'])}")
            if m["consumed_by"]:
                lines.append(f"**Consumed by**: {', '.join(f'`{s}`' for s in m['consumed_by'])}")
            lines.append("")
        if d["kafka_topic_impact"]:
            lines += ["## Kafka Topic Impact"]
            for k in d["kafka_topic_impact"]:
                lines.append(f"- Topic `{k['topic']}` produced by `{k['producer_service']}` — {k['note']}")
            lines.append("")
        lines += ["## Action Plan"]
        for rec in d["recommendation"]:
            lines.append(f"- {rec}")
        return "\n".join(lines)
    def explain_exception_flow(self, exception_name: str) -> ToolResponse:
        """Where is this exception thrown, caught, and how is it mapped to HTTP status."""
        all_nodes_data = self.graph.all_nodes()
        exc_nodes = [nd for nd in all_nodes_data
                     if nd.get("type") == "ExceptionType" and
                     exception_name.lower() in nd.get("name", "").lower()]

        throwers: list[dict] = []
        for n in all_nodes_data:
            throws = n.get("attributes", {}).get("thrown_exceptions", [])
            if any(exception_name.lower() in str(t).lower() for t in throws):
                throwers.append({
                    "class": n.get("attributes", {}).get("class") or n.get("name"),
                    "method": n.get("name") if n.get("type") == "Method" else None,
                    "type": n.get("type"),
                    "service": n.get("service"),
                    "file": n.get("attributes", {}).get("file"),
                    "lines": (f"{n.get('attributes',{}).get('start_line')}-"
                              f"{n.get('attributes',{}).get('end_line')}"),
                })

        retrieval = self.retriever.retrieve(
            f"{exception_name} ExceptionHandler ControllerAdvice catch",
            collections=("code",), top_k=8,
        )
        handler_chunks = [rc for rc in retrieval.chunks
                          if any(kw in rc.chunk.text
                                 for kw in ("ExceptionHandler", "ControllerAdvice",
                                            exception_name))]
        exc_attrs = exc_nodes[0].get("attributes", {}) if exc_nodes else {}
        http_status = (exc_attrs.get("http_status") or
                       self._exception_to_status(exception_name))

        data = {
            "exception": exception_name, "http_status": http_status,
            "defined_in": exc_attrs.get("file"),
            "extends": exc_attrs.get("extends", []),
            "thrown_by": throwers,
            "handler_code": [rc.chunk.text[:600] for rc in handler_chunks[:3]],
            "total_throw_sites": len(throwers),
        }
        lines = [f"# Exception Flow: `{exception_name}`",
                 f"**HTTP Status**: `{http_status}`",
                 f"**Defined in**: `{exc_attrs.get('file', 'not indexed')}`",
                 f"**Extends**: {', '.join(f'`{e}`' for e in exc_attrs.get('extends', []))}",
                 f"**Thrown by**: {len(throwers)} method(s)", ""]
        if throwers:
            lines.append("## Throw Sites")
            for t in throwers[:15]:
                m_part = f".{t['method']}" if t.get("method") else ""
                lines.append(f"- `{t['class']}{m_part}` [{t['type']}] "
                              f"in `{t['service']}` ({t['file']})")
            lines.append("")
        if handler_chunks:
            lines.append("## Exception Handler")
            for rc in handler_chunks[:2]:
                lines += [f"**`{rc.chunk.rel_path}`**",
                          f"```java\n{rc.chunk.text[:800]}\n```", ""]
        return ToolResponse(
            tool="explain_exception_flow",
            query={"exception_name": exception_name},
            summary=(f"`{exception_name}` → HTTP {http_status}. "
                     f"Thrown at {len(throwers)} site(s)."),
            data=data, markdown="\n".join(lines),
            citations=citations_from(retrieval.chunks),
        )
    def get_defect_changes(self, ticket_id: str,
                           file_filter: str = "java") -> ToolResponse:
        """Find all branches for a defect/story. file_filter='java' returns only Java source
        diffs (default, ~80% fewer tokens); 'all' includes config/build files too."""
        from git import GitCommandError, InvalidGitRepositoryError, Repo

        ticket_upper = ticket_id.strip().upper()
        repos_root = Path(self.settings.repos_root)

        all_repo_results: list[dict] = []

        for repo_dir in sorted(repos_root.iterdir()):
            if not repo_dir.is_dir():
                continue
            try:
                git_repo = Repo(str(repo_dir))
            except InvalidGitRepositoryError:
                continue

            # Skip network fetch — remote-tracking refs from ingestion are sufficient.
            # (fetch blocks on auth/network and causes MCP timeout → terminal fallback)

            # Collect matching branch names, preferring full remote-tracking refs.
            matching_branches: list[str] = []
            seen_refs: set[str] = set()
            try:
                # Collect remote-tracking refs directly (origin/DE...) — no local checkout needed.
                for remote in git_repo.remotes:
                    try:
                        for ref in remote.refs:
                            if ref.name.upper().split("/", 1)[-1].startswith(ticket_upper):
                                if ref.name not in seen_refs:
                                    seen_refs.add(ref.name)
                                    matching_branches.append(ref.name)
                    except Exception:
                        pass
                # Fall back to local branches if no remote refs found.
                if not matching_branches:
                    for ref in git_repo.heads:
                        if ref.name.upper().startswith(ticket_upper):
                            if ref.name not in seen_refs:
                                seen_refs.add(ref.name)
                                matching_branches.append(ref.name)
            except Exception:
                pass

            if not matching_branches:
                continue

            repo_data: dict = {"repo": repo_dir.name, "branches": []}

            for branch_name in matching_branches:
                # branch_name is already a full ref (origin/DE...) — no further resolution.
                ref_name = branch_name

                # Find merge base with main/master so we diff only branch-specific commits.
                base_commit = None
                for base_ref in ("main", "master", "develop", "development",
                                 "origin/main", "origin/master", "origin/develop"):
                    try:
                        base_commit = git_repo.merge_base(ref_name, base_ref)
                        if base_commit:
                            base_commit = base_commit[0]
                            break
                    except Exception:
                        pass

                # Gather commits on the branch since the merge base — all via GitPython.
                commits_data: list[dict] = []
                changed_files_all: dict[str, dict] = {}  # path → {added, removed, change}
                try:
                    commit_obj = git_repo.commit(ref_name)  # noqa: F841
                    rev_range = (f"{base_commit.hexsha}..{ref_name}"
                                 if base_commit else ref_name)
                    commits_iter = list(git_repo.iter_commits(rev_range, max_count=50))

                    # Build commit list using git log (fast, no per-commit diff overhead).
                    for c in commits_iter:
                        # Use git diff-tree to get changed file paths — much faster than Diff API.
                        try:
                            name_status = git_repo.git.diff_tree(
                                "--no-commit-id", "-r", "--name-status", c.hexsha
                            )
                            file_paths = [
                                line.split("\t", 1)[-1].strip()
                                for line in name_status.splitlines() if "\t" in line
                            ]
                        except Exception:
                            file_paths = []
                        commits_data.append({
                            "sha": c.hexsha[:10],
                            "author": str(c.author),
                            "date": c.committed_datetime.strftime("%Y-%m-%d %H:%M"),
                            "message": c.message.strip().split("\n")[0][:120],
                            "files_changed": file_paths,
                        })

                    # Get the full unified diff for the whole branch in one git call.
                    try:
                        if base_commit:
                            full_diff = git_repo.git.diff(
                                base_commit.hexsha, ref_name,
                                "--unified=6", "--diff-filter=ACMR"
                            )
                        else:
                            full_diff = git_repo.git.show(
                                ref_name, "--unified=6", "--diff-filter=ACMR"
                            )
                    except Exception as de:
                        full_diff = f"# diff unavailable: {de}"

                    # Parse unified diff into per-file structured hunks (before/after blocks).
                    def _parse_unified_diff(raw: str) -> dict[str, dict]:
                        """Split raw unified diff into {file_path: {change, hunks:[{header,before,after}]}}."""
                        files: dict[str, dict] = {}
                        cur_file: str | None = None
                        cur_change = "M"
                        cur_hunks: list[dict] = []
                        cur_hunk: dict | None = None

                        def _save_hunk() -> None:
                            if cur_hunk and cur_file:
                                cur_hunks.append(cur_hunk)

                        def _save_file() -> None:
                            if cur_file:
                                _save_hunk()
                                total_added = sum(
                                    len(h["after_lines"]) for h in cur_hunks)
                                total_removed = sum(
                                    len(h["before_lines"]) for h in cur_hunks)
                                files[cur_file] = {
                                    "path": cur_file, "change": cur_change,
                                    "lines_added": total_added,
                                    "lines_removed": total_removed,
                                    "hunks": cur_hunks[:],
                                }

                        for raw_line in raw.splitlines():
                            if raw_line.startswith("diff --git "):
                                _save_file()
                                parts = raw_line.split(" b/", 1)
                                cur_file = parts[-1].strip() if len(parts) > 1 else raw_line
                                cur_change = "M"
                                cur_hunks = []
                                cur_hunk = None
                            elif raw_line.startswith("new file"):
                                cur_change = "A"
                            elif raw_line.startswith("deleted file"):
                                cur_change = "D"
                            elif raw_line.startswith("rename"):
                                cur_change = "R"
                            elif raw_line.startswith("@@ "):
                                if cur_hunk:
                                    cur_hunks.append(cur_hunk)
                                cur_hunk = {
                                    "header": raw_line,
                                    "before_lines": [],  # removed lines (without leading -)
                                    "after_lines": [],   # added lines (without leading +)
                                    "context_lines": [], # unchanged context lines
                                }
                            elif cur_hunk is not None:
                                if raw_line.startswith("---") or raw_line.startswith("+++"):
                                    pass  # skip file headers inside hunk area
                                elif raw_line.startswith("-"):
                                    cur_hunk["before_lines"].append(raw_line[1:])
                                elif raw_line.startswith("+"):
                                    cur_hunk["after_lines"].append(raw_line[1:])
                                else:
                                    # Context line — belongs to both before and after
                                    ctx = raw_line.removeprefix(" ")
                                    cur_hunk["context_lines"].append(ctx)
                        _save_file()
                        return files

                    parsed_files = _parse_unified_diff(full_diff)
                    for fp, fdata in parsed_files.items():
                        changed_files_all[fp] = fdata

                except GitCommandError as e:
                    commits_data.append({"error": f"GitCommandError: {e}",
                                         "files_changed": []})

                # Cross-reference changed Java files with knowledge graph.
                java_files = [p for p in changed_files_all if p.endswith(".java")]
                affected_endpoints: list[dict] = []
                affected_services: set[str] = set()
                affected_methods: list[dict] = []
                for jf in java_files:
                    for n in self.graph.find_by_file(jf):
                        attrs = n.get("attributes", {})
                        nfile = attrs.get("file") or n.get("file") or ""
                        if not nfile.endswith(jf.split("/")[-1]):
                            continue
                        ntype = n.get("type", "")
                        svc = n.get("service") or attrs.get("service", "")
                        if svc:
                            affected_services.add(svc)
                        if ntype == "Endpoint":
                            affected_endpoints.append({
                                "method": attrs.get("http_method", ""),
                                "path": attrs.get("path", ""),
                                "handler": n.get("name", ""),
                                "service": svc,
                            })
                        elif ntype == "Method":
                            # Embed the method's purpose + signature so no follow-up call needed.
                            purpose = _infer_method_purpose(
                                n.get("name", ""),
                                attrs.get("called_methods") or [],
                                attrs.get("throws") or [],
                                bool(attrs.get("transactional")),
                            )
                            affected_methods.append({
                                "class": attrs.get("class_name", ""),
                                "method": n.get("name", ""),
                                "service": svc,
                                "start_line": attrs.get("start_line"),
                                "end_line": attrs.get("end_line"),
                                "purpose": purpose,
                                "parameters": attrs.get("parameters") or [],
                                "returns": attrs.get("return_type", ""),
                                "transactional": bool(attrs.get("transactional")),
                            })

                repo_data["branches"].append({
                    "branch": branch_name,
                    "commits": commits_data,
                    "total_commits": len(commits_data),
                    "changed_files": list(changed_files_all.values()),
                    "total_files": len(changed_files_all),
                    "java_files_changed": java_files,
                    "affected_endpoints": affected_endpoints,
                    "affected_methods": affected_methods[:20],
                    "affected_services": sorted(affected_services),
                })

            all_repo_results.append(repo_data)

        if not all_repo_results:
            return ToolResponse(
                tool="get_defect_changes",
                query={"ticket_id": ticket_id},
                summary=f"No branches found matching '{ticket_id}' in any repo.",
                data={"ticket_id": ticket_id, "repos": []},
                markdown=f"## No branches found for `{ticket_id}`\n\nNo git branches in the knowledge base start with `{ticket_id}`.",
            )

        md = self._render_defect_changes_md(ticket_id, all_repo_results,
                                             file_filter=file_filter)
        total_commits = sum(b["total_commits"] for r in all_repo_results for b in r["branches"])
        total_files   = sum(b["total_files"]   for r in all_repo_results for b in r["branches"])
        summary = (f"Ticket {ticket_id}: {len(all_repo_results)} repo(s), "
                   f"{sum(len(r['branches']) for r in all_repo_results)} branch(es), "
                   f"{total_commits} commit(s), {total_files} file(s) changed.")
        return ToolResponse(
            tool="get_defect_changes",
            query={"ticket_id": ticket_id},
            summary=summary,
            data={"ticket_id": ticket_id, "repos": all_repo_results},
            markdown=md,
        )
    @staticmethod
    def _render_defect_changes_md(ticket_id: str, repos: list[dict],
                                  file_filter: str = "java") -> str:
        prefix = "Defect" if ticket_id.upper().startswith("DE") else "User Story"
        lines = [f"# {prefix} `{ticket_id}`", ""]

        for repo_data in repos:
            for br in repo_data["branches"]:
                lines += [f"**Branch:** `{br['branch']}` · `{repo_data['repo']}`",
                          f"**Commits:** {br['total_commits']}  "
                          f"**Files changed:** {br['total_files']}", ""]

                # Compact commit list — one line each
                for c in br["commits"]:
                    if "error" not in c:
                        lines.append(f"- `{c['sha']}` `{c['date']}` *{c['author']}* — {c['message']}")
                lines.append("")

                # Affected endpoints + methods in two compact lines
                if br["affected_endpoints"]:
                    eps = " · ".join(f"`{e['method']} {e['path']}`"
                                     for e in br["affected_endpoints"])
                    lines.append(f"**Endpoints:** {eps}")
                if br["affected_methods"]:
                    mths = " · ".join(
                        f"`{m['class']}.{m['method']}`"
                        + (f":{m['start_line']}" if m.get("start_line") else "")
                        for m in br["affected_methods"]
                    )
                    lines.append(f"**Methods:** {mths}")
                lines.append("")

                # Split into java vs other files
                all_files = br["changed_files"]
                if file_filter == "java":
                    show_files  = [f for f in all_files if f["path"].endswith(".java")]
                    other_files = [f for f in all_files if not f["path"].endswith(".java")]
                else:
                    show_files  = all_files
                    other_files = []

                # Other (non-java) files: compact one-line-per-file table
                if other_files:
                    lines.append("**Other files changed** (use `file_filter='all'` to see diffs):")
                    lines.append("| Change | File | +Lines | -Lines |")
                    lines.append("|--------|------|-------:|-------:|")
                    for f in sorted(other_files, key=lambda x: x["path"]):
                        icon = {"A": "Added", "M": "Modified", "D": "Deleted",
                                "R": "Renamed"}.get(f["change"], f["change"])
                        lines.append(f"| {icon} | `{f['path']}` | "
                                     f"+{f['lines_added']} | -{f['lines_removed']} |")
                    lines.append("")

                # Java files: full unified diff per hunk, no limits
                lines.append("---")
                lines.append("## Code Changes")
                for f in sorted(show_files, key=lambda x: x["path"]):
                    icon = {"A": "Added", "M": "Modified", "D": "Deleted",
                            "R": "Renamed"}.get(f["change"], f["change"])
                    lines += [
                        f"### `{f['path']}` — {icon} "
                        f"(`+{f['lines_added']}` / `-{f['lines_removed']}`)",
                        "",
                    ]
                    for hunk in f.get("hunks", []):
                        before = hunk.get("before_lines", [])
                        after  = hunk.get("after_lines",  [])
                        ctx    = hunk.get("context_lines", [])
                        if not before and not after:
                            continue
                        lines.append(f"`{hunk.get('header', '')}`")
                        lines.append("```diff")
                        # context lines (first 3)
                        for c in ctx[:3]:
                            lines.append(f" {c}")
                        # removed lines
                        for b in before:
                            lines.append(f"-{b}")
                        # added lines
                        for a in after:
                            lines.append(f"+{a}")
                        # context lines (last 3)
                        for c in ctx[-3:] if len(ctx) > 3 else []:
                            lines.append(f" {c}")
                        lines.append("```")
                        lines.append("")

        return "\n".join(lines)
    def analyse_exception(
        self, stack_trace: str | None = None, exception_type: str | None = None,
        message: str | None = None, service_name: str | None = None,
        version: str | None = None, environment: str | None = None,
        logs: str | None = None,
        customer_impact: str | None = None, revenue_impact: str | None = None,
        frequency_per_hour: float | None = None, services_affected: int | None = None,
        recovery_time_minutes: float | None = None,
    ) -> ToolResponse:
        """Root-cause a production exception: stack trace -> graph correlation
        -> severity -> related historical incidents -> suggested fix. Persists
        the result as a first-class incident for future similarity search."""
        event, rca = analyze_exception(
            graph=self.graph, stack_trace=stack_trace, exception_type=exception_type,
            message=message, service_name=service_name, version=version,
            environment=environment, logs=logs,
            incident_source=self.incidents,
        )
        severity = classify_severity(
            customer_impact=customer_impact, revenue_impact=revenue_impact,
            frequency_per_hour=frequency_per_hour, services_affected=services_affected
            or len(rca.affected_services) or None,
            recovery_time_minutes=recovery_time_minutes,
            historical_pattern_similarity=(rca.related_incidents[0]["score"]
                                           if rca.related_incidents else None),
        )
        rca.severity = severity
        from ...models import IncidentRecord

        root_frame = event.stack_frames[0] if event.stack_frames else None
        incident_metadata = {
            "related_code_areas": rca.related_code_areas,
            "evidence": rca.evidence,
            "related_incidents": rca.related_incidents,
            "extracted": {
                "exception_type": event.exception_type,
                "class": root_frame.class_name if root_frame else None,
                "method": root_frame.method_name if root_frame else None,
                "service": event.service_name,
                "version": event.version,
                "environment": event.environment,
            },
        }

        incident = self.incidents.save(IncidentRecord(
            id="", title=f"{event.exception_type} in {service_name or 'unknown service'}",
            description=rca.probable_root_cause, severity=severity.severity,
            service_name=event.service_name, environment=event.environment,
            version=event.version, exception_type=event.exception_type,
            exception_class=root_frame.class_name if root_frame else None,
            exception_method=root_frame.method_name if root_frame else None,
            stack_trace=stack_trace, logs=logs,
            root_cause=rca.probable_root_cause,
            rca_document=(
                f"Root Cause Analysis\n\nCause: {rca.probable_root_cause}\n\n"
                f"Evidence: {rca.evidence}\n\nSuggested Fix: {rca.suggested_fix}"
            ),
            confidence=rca.confidence, fix_summary=rca.suggested_fix,
            resolution=rca.suggested_fix,
            affected_services=rca.affected_services,
            metadata=incident_metadata,
        ))
        data = {
            "incident_id": incident.id, "root_cause": rca.probable_root_cause,
            "confidence": rca.confidence, "severity": severity.model_dump(mode="json"),
            "extracted": {
                "exception_type": event.exception_type,
                "class": root_frame.class_name if root_frame else None,
                "method": root_frame.method_name if root_frame else None,
                "service": event.service_name,
                "version": event.version,
                "environment": event.environment,
            },
            "affected_services": rca.affected_services, "suggested_fix": rca.suggested_fix,
            "related_code_areas": rca.related_code_areas,
            "related_incidents": rca.related_incidents, "evidence": rca.evidence,
        }
        lines = [
            f"# Exception Analysis: `{event.exception_type}`", "",
            f"**Severity: {severity.severity.value}** (confidence {rca.confidence:.0%})", "",
            f"## Probable Root Cause\n{rca.probable_root_cause}", "",
            f"## Suggested Fix\n{rca.suggested_fix}", "",
        ]
        if rca.related_code_areas:
            lines.append("## Related Code Areas")
            for a in rca.related_code_areas:
                lines.append(f"- `{a['class']}.{a['method']}` ({a['file']}:{a['line']}) "
                             f"[{a.get('service') or 'unresolved'}]")
        if rca.related_incidents:
            lines.append("\n## Related Historical Incidents")
            for inc in rca.related_incidents:
                lines.append(f"- `{inc.get('title')}` (severity={inc.get('severity')}, "
                             f"score={inc.get('score')})")
        lines.append("\n" + severity.as_markdown())
        return ToolResponse(
            tool="analyse_exception",
            query={"exception_type": exception_type, "service_name": service_name,
                   "version": version},
            summary=f"{severity.severity.value}: {rca.probable_root_cause[:160]}",
            data=data, markdown="\n".join(lines),
        )
    def classify_incident(
        self, description: str, service_name: str | None = None,
        customer_impact: str | None = None, revenue_impact: str | None = None,
        frequency_per_hour: float | None = None, services_affected: int | None = None,
        recovery_time_minutes: float | None = None,
        historical_pattern_similarity: float | None = None,
    ) -> ToolResponse:
        """Classify an incident P1-P4 using explainable, rule-based hybrid
        scoring (never LLM-only) — see rca/severity_scoring.py."""
        similar = self.incidents.find_similar(description, top_k=5)
        if historical_pattern_similarity is None and similar:
            historical_pattern_similarity = similar[0].get("score")
            if historical_pattern_similarity and historical_pattern_similarity > 1.0:
                historical_pattern_similarity = min(historical_pattern_similarity / 10.0, 1.0)
        severity = classify_severity(
            customer_impact=customer_impact, revenue_impact=revenue_impact,
            frequency_per_hour=frequency_per_hour, services_affected=services_affected,
            recovery_time_minutes=recovery_time_minutes,
            historical_pattern_similarity=historical_pattern_similarity,
        )
        from ...models import IncidentRecord

        incident = self.incidents.save(IncidentRecord(
            id="", title=description[:160], description=description,
            severity=severity.severity, service_name=service_name,
            confidence=severity.confidence,
            affected_services=[service_name] if service_name else [],
        ))
        data = {"incident_id": incident.id, "severity": severity.model_dump(mode="json"),
                "related_incidents": similar}
        md = f"# Incident Classification\n\n{severity.as_markdown()}\n"
        if similar:
            md += "\n## Similar Historical Incidents\n"
            for s in similar:
                md += f"- `{s.get('title')}` (severity={s.get('severity')}, score={s.get('score')})\n"
        return ToolResponse(
            tool="classify_incident", query={"description": description},
            summary=f"{severity.severity.value} (confidence {severity.confidence:.0%}).",
            data=data, markdown=md,
        )
    def find_related_incidents(self, query: str) -> ToolResponse:
        """Retrieve previously-resolved incidents similar to `query` (an
        exception type, symptom description, or free-text search)."""
        similar = self.incidents.find_similar(
            query, top_k=self.settings.incident_similarity_top_k)
        data = {"query": query, "count": len(similar), "incidents": similar}
        lines = [f"# Related Incidents: _{query}_", "", f"**{len(similar)} found.**", ""]
        for s in similar:
            lines.append(f"- `{s.get('title')}` (severity={s.get('severity')}, "
                         f"service={s.get('service')}, score={s.get('score')})")
            if s.get("excerpt"):
                lines.append(f"  > {s['excerpt'][:200]}")
        if not similar:
            lines.append("_No similar historical incidents found._")
        return ToolResponse(
            tool="find_related_incidents", query={"query": query},
            summary=f"{len(similar)} related incident(s) found.",
            data=data, markdown="\n".join(lines),
        )
