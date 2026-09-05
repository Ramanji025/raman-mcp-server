"""KnowledgeService mixin: QualityMixin (quality domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...retrieval.rag import citations_from

log = get_logger(__name__)




class QualityMixin:
    """security_audit / dependency_report / schema_analysis / test_coverage_map tools."""
    def security_audit(self, service_name: str) -> ToolResponse:
        """Audit a service for security gaps: unprotected endpoints, hardcoded secrets, CORS."""
        svc = self._resolve_service(service_name)
        endpoints   = self.graph.nodes_by_type(NodeType.ENDPOINT, svc)
        configs     = self.graph.nodes_by_type(NodeType.CONFIG, svc)
        svc_layers  = self.graph.nodes_by_type(NodeType.SERVICE_LAYER, svc)
        controllers = self.graph.nodes_by_type(NodeType.CONTROLLER, svc)

        unprotected: list[dict] = []
        for ep in endpoints:
            attrs = ep.get("attributes", {})
            if not attrs.get("security"):
                unprotected.append({
                    "endpoint": ep.get("name"), "method": attrs.get("http_method"),
                    "path": attrs.get("path"), "controller": attrs.get("controller"),
                    "file": attrs.get("file"),
                    "recommendation": "Add @PreAuthorize or configure in SecurityConfig",
                })

        hardcoded_secrets: list[dict] = []
        for cfg in configs:
            attrs = cfg.get("attributes", {})
            for key in attrs.get("hardcoded_secrets", []):
                hardcoded_secrets.append({
                    "service": svc, "config_key": key, "file": attrs.get("file"),
                    "recommendation": "Move to environment variable or Vault secret",
                })

        antipattern_smells: list[dict] = []
        for cls in svc_layers + controllers:
            for smell in cls.get("attributes", {}).get("antipatterns", []):
                antipattern_smells.append({"class": cls.get("name"), "issue": smell})

        # Check for @ControllerAdvice presence.
        all_nodes = self.graph.all_nodes(svc)
        has_advice = any("ControllerAdvice" in str(n.get("attributes", {}).get("fqcn", "")) or
                         "ExceptionHandler" in str(n.get("attributes", {}))
                         for n in all_nodes)

        risk_score = (len(unprotected) * 3 + len(hardcoded_secrets) * 5 +
                      len(antipattern_smells))
        risk_level = ("CRITICAL" if risk_score > 20 else
                      "HIGH" if risk_score > 10 else
                      "MEDIUM" if risk_score > 3 else "LOW")

        data = {
            "service": svc,
            "risk_level": risk_level, "risk_score": risk_score,
            "unprotected_endpoints": unprotected,
            "unprotected_count": len(unprotected),
            "total_endpoints": len(endpoints),
            "hardcoded_secrets": hardcoded_secrets,
            "has_global_exception_handler": has_advice,
            "antipattern_smells": antipattern_smells,
            "recommendations": self._security_recommendations(
                unprotected, hardcoded_secrets, has_advice),
        }
        md = self._render_security_audit_md(svc, data)
        return ToolResponse(
            tool="security_audit", query={"service_name": service_name},
            summary=(f"Security audit for {svc}: {risk_level} risk. "
                     f"{len(unprotected)}/{len(endpoints)} endpoints unprotected, "
                     f"{len(hardcoded_secrets)} hardcoded secrets."),
            data=data, markdown=md,
        )
    @staticmethod
    def _security_recommendations(unprotected, secrets, has_advice) -> list[str]:
        recs = []
        if unprotected:
            recs.append(f"Protect {len(unprotected)} endpoint(s) with @PreAuthorize or SecurityConfig rules.")
        if secrets:
            recs.append(f"Remove {len(secrets)} hardcoded secret(s) — use Spring Cloud Config / Vault.")
        if not has_advice:
            recs.append("Add a @ControllerAdvice class for centralised exception → HTTP status mapping.")
        recs.append("Enable HTTPS in production (server.ssl.* properties).")
        recs.append("Audit CORS configuration (WebMvcConfigurer.addCorsMappings).")
        recs.append("Enable CSRF protection for browser-facing APIs.")
        recs.append("Review Spring Security OAuth2 / JWT token expiry and refresh strategy.")
        return recs
    def dependency_report(self, service_name: str) -> ToolResponse:
        """Full Maven dependency tree with CVE flags, scopes, and Spring Boot version."""
        svc = self._resolve_service(service_name)

        # Find SERVICE node which carries the dep list.
        svc_nodes = self.graph.find_nodes(svc, {NodeType.SERVICE}, limit=5)
        svc_node = next((n for n in svc_nodes if n.get("service") == svc), None)
        svc_attrs = svc_node.get("attributes", {}) if svc_node else {}

        # Also collect individual DEPENDENCY nodes.
        dep_nodes = self.graph.nodes_by_type(NodeType.DEPENDENCY, svc)

        all_deps = svc_attrs.get("dependencies", []) or [
            {"artifactId": n.get("name"), **n.get("attributes", {})} for n in dep_nodes
        ]
        cve_flagged = [d for d in all_deps if d.get("cve_note")]
        compile_deps = [d for d in all_deps if d.get("scope", "compile") == "compile"]
        test_deps    = [d for d in all_deps if d.get("scope") == "test"]
        runtime_deps = [d for d in all_deps if d.get("scope") == "runtime"]

        data = {
            "service": svc,
            "spring_boot_version": svc_attrs.get("spring_boot_version", "unknown"),
            "java_version": svc_attrs.get("java_version", "unknown"),
            "total_dependencies": len(all_deps),
            "compile_count": len(compile_deps),
            "test_count": len(test_deps),
            "runtime_count": len(runtime_deps),
            "cve_flagged_count": len(cve_flagged),
            "cve_flagged": cve_flagged,
            "all_dependencies": all_deps,
        }
        md = self._render_dependency_report_md(svc, data)
        return ToolResponse(
            tool="dependency_report", query={"service_name": service_name},
            summary=(f"Dependency report for {svc}: {len(all_deps)} deps, "
                     f"{len(cve_flagged)} CVE-flagged, "
                     f"Spring Boot {svc_attrs.get('spring_boot_version', '?')}."),
            data=data, markdown=md,
        )
    def schema_analysis(self, service_name: str) -> ToolResponse:
        """JPA entity → DB schema: columns, relationships, N+1 risks, migration scripts."""
        svc = self._resolve_service(service_name)
        entities = self.graph.nodes_by_type(NodeType.ENTITY, svc)
        tables   = self.graph.nodes_by_type(NodeType.TABLE, svc)

        schema: list[dict] = []
        all_antipatterns: list[dict] = []
        for ent in entities:
            attrs = ent.get("attributes", {})
            columns = attrs.get("columns", [])
            rels    = attrs.get("relationships", [])
            ap      = attrs.get("antipatterns", [])
            table_name = next((t.get("name") for t in tables
                               if ent.get("name", "").lower() in t.get("name", "").lower()), "?")
            schema.append({
                "entity": ent.get("name"),
                "table": table_name,
                "file": attrs.get("file"),
                "columns": columns,
                "relationships": rels,
                "antipatterns": ap,
            })
            for a in ap:
                all_antipatterns.append({"entity": ent.get("name"), "issue": a})

        # Retrieval for migration context.
        retrieval = self.retriever.retrieve(
            f"Flyway Liquibase migration schema {svc}", top_k=5,
        )

        data = {
            "service": svc,
            "entity_count": len(entities),
            "table_count": len(tables),
            "schema": schema,
            "schema_antipatterns": all_antipatterns,
            "has_migrations": any("migration" in rc.chunk.rel_path.lower() or
                                   "flyway" in rc.chunk.rel_path.lower() or
                                   "liquibase" in rc.chunk.rel_path.lower()
                                   for rc in retrieval.chunks),
        }
        md = self._render_schema_analysis_md(svc, data)
        return ToolResponse(
            tool="schema_analysis", query={"service_name": service_name},
            summary=(f"Schema analysis for {svc}: {len(entities)} entities, "
                     f"{len(all_antipatterns)} schema antipatterns."),
            data=data, markdown=md, citations=citations_from(retrieval.chunks),
        )
    def find_antipatterns(self, service_name: str) -> ToolResponse:
        """Detect code smells: God classes, missing @Valid, field injection, N+1, etc."""
        svc = self._resolve_service(service_name)
        node_types = [NodeType.CONTROLLER, NodeType.SERVICE_LAYER, NodeType.REPOSITORY,
                      NodeType.ENTITY, NodeType.DTO]
        all_smells: list[dict] = []

        for nt in node_types:
            for node in self.graph.nodes_by_type(nt, svc):
                smells = node.get("attributes", {}).get("antipatterns", [])
                for smell in smells:
                    all_smells.append({
                        "class": node.get("name"), "type": str(nt),
                        "file": node.get("attributes", {}).get("file"),
                        "issue": smell,
                        "severity": self._smell_severity(smell),
                    })

        # Sort by severity.
        severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        all_smells.sort(key=lambda x: severity_order.get(x["severity"], 3))

        data = {
            "service": svc,
            "total_smells": len(all_smells),
            "high_severity": [s for s in all_smells if s["severity"] == "HIGH"],
            "medium_severity": [s for s in all_smells if s["severity"] == "MEDIUM"],
            "low_severity": [s for s in all_smells if s["severity"] == "LOW"],
            "all_smells": all_smells,
        }
        md = self._render_antipatterns_md(svc, data)
        return ToolResponse(
            tool="find_antipatterns", query={"service_name": service_name},
            summary=(f"Found {len(all_smells)} code smells in {svc}: "
                     f"{len(data['high_severity'])} HIGH, "
                     f"{len(data['medium_severity'])} MEDIUM."),
            data=data, markdown=md,
        )
    @staticmethod
    def _render_security_audit_md(svc: str, d: dict) -> str:
        rl = d["risk_level"]
        emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(rl, "⚪")
        lines = [f"# Security Audit — `{svc}`",
                 f"**Risk Level: {emoji} {rl}** (score: {d['risk_score']})", ""]
        lines += ["## Unprotected Endpoints",
                  f"*{d['unprotected_count']} of {d['total_endpoints']} endpoints lack @PreAuthorize*", ""]
        for ep in d["unprotected_endpoints"]:
            lines.append(f"- `{ep['method']} {ep['path']}` in `{ep['controller']}` — {ep['recommendation']}")
        if d["hardcoded_secrets"]:
            lines += ["", "## Hardcoded Secrets ⚠️"]
            for s in d["hardcoded_secrets"]:
                lines.append(f"- `{s['config_key']}` in `{s['file']}` — {s['recommendation']}")
        lines += ["", f"## Global Exception Handler: {'✅ present' if d['has_global_exception_handler'] else '❌ missing'}"]
        if d["antipattern_smells"]:
            lines += ["", "## Security-Related Code Smells"]
            for smell in d["antipattern_smells"]:
                lines.append(f"- `{smell['class']}`: {smell['issue']}")
        lines += ["", "## Recommendations"]
        for rec in d["recommendations"]:
            lines.append(f"- {rec}")
        return "\n".join(lines)
    @staticmethod
    def _render_dependency_report_md(svc: str, d: dict) -> str:
        lines = [f"# Dependency Report — `{svc}`",
                 f"**Spring Boot**: {d['spring_boot_version']} | **Java**: {d['java_version']}",
                 f"**Total**: {d['total_dependencies']} deps "
                 f"({d['compile_count']} compile, {d['test_count']} test, {d['runtime_count']} runtime)", ""]
        if d["cve_flagged"]:
            lines += [f"## ⚠️ CVE-Flagged Libraries ({d['cve_flagged_count']})"]
            for dep in d["cve_flagged"]:
                lines.append(f"- **`{dep['artifactId']}`** {dep.get('version','')} — {dep['cve_note']}")
            lines.append("")
        lines += ["## All Dependencies"]
        for dep in d["all_dependencies"]:
            cve = f" ⚠️ {dep['cve_note']}" if dep.get("cve_note") else ""
            lines.append(f"- `{dep.get('groupId','')}:{dep['artifactId']}` "
                         f"v{dep.get('version','?')} [{dep.get('scope','compile')}]{cve}")
        return "\n".join(lines)
    @staticmethod
    def _render_schema_analysis_md(svc: str, d: dict) -> str:
        lines = [f"# Schema Analysis — `{svc}`",
                 f"**{d['entity_count']} entities**, **{d['table_count']} tables**", ""]
        for ent in d["schema"]:
            lines += [f"## `{ent['entity']}` → table `{ent['table']}`"]
            if ent["columns"]:
                lines.append("**Columns:**")
                for col in ent["columns"]:
                    pk = " (PK)" if col.get("primary_key") else ""
                    lines.append(f"  - `{col['field']}`: {col['java_type']}{pk}")
            if ent["relationships"]:
                lines.append("**Relationships:**")
                for rel in ent["relationships"]:
                    lines.append(f"  - `{rel['field']}` → `{rel['target_entity']}` "
                                 f"[{rel['type']}, fetch={rel['fetch']}]")
            if ent["antipatterns"]:
                lines.append("**⚠️ Schema Issues:**")
                for ap in ent["antipatterns"]:
                    lines.append(f"  - {ap}")
            lines.append("")
        if d["schema_antipatterns"]:
            lines += ["## All Schema Antipatterns"]
            for ap in d["schema_antipatterns"]:
                lines.append(f"- `{ap['entity']}`: {ap['issue']}")
        return "\n".join(lines)
    @staticmethod
    def _render_antipatterns_md(svc: str, d: dict) -> str:
        lines = [f"# Code Smell Report — `{svc}`",
                 f"**{d['total_smells']} issues**: "
                 f"{len(d['high_severity'])} HIGH, "
                 f"{len(d['medium_severity'])} MEDIUM, "
                 f"{len(d['low_severity'])} LOW", ""]
        for severity, items in [("HIGH 🔴", d["high_severity"]),
                                  ("MEDIUM 🟡", d["medium_severity"]),
                                  ("LOW 🟢", d["low_severity"])]:
            if items:
                lines.append(f"## {severity}")
                for s in items:
                    lines.append(f"- `{s['class']}` [{s['type']}]: {s['issue']}")
                lines.append("")
        return "\n".join(lines)
    def test_coverage_map(self, service_name: str) -> ToolResponse:
        """Map which classes have test coverage and identify untested endpoints."""
        svc = self._resolve_service(service_name)
        test_nodes   = self.graph.nodes_by_type(NodeType.TEST_CLASS, svc)
        svc_layers   = self.graph.nodes_by_type(NodeType.SERVICE_LAYER, svc)
        controllers  = self.graph.nodes_by_type(NodeType.CONTROLLER, svc)
        repositories = self.graph.nodes_by_type(NodeType.REPOSITORY, svc)

        tested_classes = {t.get("attributes", {}).get("tested_class") for t in test_nodes
                          if t.get("attributes", {}).get("tested_class")}

        by_type: dict[str, list[dict]] = {}
        for t in test_nodes:
            ttype = t.get("attributes", {}).get("test_type", "unit")
            by_type.setdefault(ttype, []).append({
                "test_class": t.get("name"),
                "tests": t.get("attributes", {}).get("tested_class"),
                "framework": t.get("attributes", {}).get("test_framework"),
            })

        untested_controllers = [c.get("name") for c in controllers
                                 if c.get("name") not in tested_classes]
        untested_services    = [s.get("name") for s in svc_layers
                                 if s.get("name") not in tested_classes]
        untested_repos       = [r.get("name") for r in repositories
                                 if r.get("name") not in tested_classes]

        coverage_pct = (len(tested_classes) /
                        max(len(controllers) + len(svc_layers) + len(repositories), 1) * 100)

        data = {
            "service": svc,
            "test_class_count": len(test_nodes),
            "estimated_coverage_pct": round(coverage_pct, 1),
            "tests_by_type": by_type,
            "untested_controllers": untested_controllers,
            "untested_services": untested_services,
            "untested_repositories": untested_repos,
            "recommendations": self._test_recommendations(
                untested_controllers, untested_services, untested_repos, by_type),
        }
        md = self._render_test_coverage_md(svc, data)
        return ToolResponse(
            tool="test_coverage_map", query={"service_name": service_name},
            summary=(f"Test coverage for {svc}: {len(test_nodes)} test classes, "
                     f"~{coverage_pct:.0f}% class coverage, "
                     f"{len(untested_controllers)} untested controllers."),
            data=data, markdown=md,
        )
    @staticmethod
    def _test_recommendations(uc, us, ur, by_type) -> list[str]:
        recs = []
        if uc:
            recs.append(f"Add @WebMvcTest for: {', '.join(uc[:5])}")
        if us:
            recs.append(f"Add @ExtendWith(MockitoExtension.class) unit tests for: {', '.join(us[:5])}")
        if ur:
            recs.append(f"Add @DataJpaTest slice tests for: {', '.join(ur[:5])}")
        if not by_type.get("integration"):
            recs.append("No integration tests found — add @SpringBootTest end-to-end tests")
        recs.append("Use Testcontainers for DB/Kafka integration tests.")
        recs.append("Aim for 80%+ line coverage with JaCoCo Maven plugin.")
        return recs
    @staticmethod
    def _render_test_coverage_md(svc: str, d: dict) -> str:
        lines = [f"# Test Coverage Map — `{svc}`",
                 f"**Test classes**: {d['test_class_count']} | "
                 f"**Estimated coverage**: ~{d['estimated_coverage_pct']}%", ""]
        for ttype, tests in d["tests_by_type"].items():
            lines.append(f"## {ttype.title()} Tests ({len(tests)})")
            for t in tests:
                lines.append(f"  - `{t['test_class']}` tests `{t['tests'] or '?'}` [{t['framework']}]")
            lines.append("")
        if d["untested_controllers"]:
            lines += ["## ❌ Untested Controllers"]
            for c in d["untested_controllers"]:
                lines.append(f"  - `{c}`")
        if d["untested_services"]:
            lines += ["", "## ❌ Untested Service Classes"]
            for s in d["untested_services"]:
                lines.append(f"  - `{s}`")
        if d["untested_repositories"]:
            lines += ["", "## ❌ Untested Repositories"]
            for r in d["untested_repositories"]:
                lines.append(f"  - `{r}`")
        lines += ["", "## Recommendations"]
        for rec in d["recommendations"]:
            lines.append(f"- {rec}")
        return "\n".join(lines)
